import requests
import hmac
import hashlib
import time
import json
import re
from typing import Dict, Optional, Union
import logging
from datetime import datetime
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BASE_URL = "https://api.coinex.com"

class RateLimitError(Exception):
    pass

class CoinexTradingBot:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = BASE_URL
        self.active_trades = {}  # To track open positions
        
        # Rate limits as per documentation
        self.rate_limits = {
            "order": {"limit": 30, "period": 1},  # 30r/1s for placing orders
            "cancel": {"limit": 60, "period": 1},  # 60r/1s for canceling orders
            "query": {"limit": 50, "period": 1},   # 50r/1s for querying orders
            "account": {"limit": 10, "period": 1}  # 10r/1s for account queries
        }

    def create_signature(self, method: str, endpoint: str, params: Dict = None) -> tuple:
        """
        Create signature according to CoinEx API v2 documentation
        Format: method + request_path + body(optional) + timestamp
        """
        timestamp = str(int(time.time() * 1000))
        
        # Construct request path with query parameters for GET requests
        if method.upper() == 'GET' and params:
            # Sort parameters by key
            sorted_params = sorted(params.items())
            # Create query string
            query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
            # Append query string to endpoint
            request_path = f"{endpoint}?{query_string}"
        else:
            request_path = endpoint

        # Prepare the string to sign according to documentation
        # Format: method + request_path + body(optional) + timestamp
        to_sign = method.upper() + request_path
        
        # Add JSON body for POST requests
        if method.upper() == 'POST' and params:
            to_sign += json.dumps(params)
            
        # Add timestamp
        to_sign += timestamp

        # Debug logging
        logger.debug(f"String to sign: {to_sign}")
        
        # Create signature using HMAC-SHA256 as per documentation
        signature = hmac.new(
            bytes(self.api_secret, 'latin-1'),  # Use raw secret key
            bytes(to_sign, 'latin-1'),
            hashlib.sha256
        ).hexdigest().lower()  # Convert to lowercase hex

        logger.debug(f"Generated signature: {signature}")
        return signature, timestamp

    def handle_rate_limits(self, response: requests.Response) -> None:
        """Handle rate limit headers and raise exception if limits are exceeded"""
        remaining = response.headers.get('X-RateLimit-Remaining')
        limit = response.headers.get('X-RateLimit-Limit')
        
        if response.status_code == 429 or (remaining and int(remaining) <= 0):
            raise RateLimitError("Rate limit exceeded. Please wait before making more requests.")
            
        # Log rate limit info
        if remaining and limit:
            logger.debug(f"Rate limit remaining: {remaining}/{limit}")
            
        # Check for long period rate limits
        for header in response.headers:
            if header.startswith('X-RateLimit-LongPeriod-'):
                period = header.split('-')[3]  # e.g., "24H"
                remaining = response.headers[header]
                logger.debug(f"Long period rate limit ({period}) remaining: {remaining}")

    def send_request(self, endpoint: str, params: Dict = None, method: str = "GET") -> Dict:
        """Send authenticated request to CoinEx API"""
        try:
            # Prepare the request
            url = f"{self.base_url}{endpoint}"
            
            # Get signature and timestamp
            signature, timestamp = self.create_signature(method, endpoint, params)

            # Prepare headers according to the documentation
            headers = {
                'X-COINEX-KEY': self.api_key,
                'X-COINEX-SIGN': signature,
                'X-COINEX-TIMESTAMP': timestamp,
                'Content-Type': 'application/json'
            }

            # Send request
            if method.upper() == 'GET':
                # For GET requests, parameters go in URL
                response = requests.get(url, params=params, headers=headers)
            else:
                # For POST requests, parameters go in JSON body
                response = requests.post(url, json=params, headers=headers)

            # Handle rate limits
            self.handle_rate_limits(response)
            
            response.raise_for_status()
            
            # Handle common error codes
            json_response = response.json()
            if json_response.get('code') in [3008, 4001, 4213]:
                logger.warning(f"Rate limit warning: {json_response.get('message')}")
                time.sleep(1)  # Basic backoff
                
            return json_response

        except RateLimitError as e:
            logger.error(f"Rate limit exceeded: {str(e)}")
            return {"code": 4213, "message": str(e)}
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {str(e)}")
            return {"code": -1, "message": f"Request failed: {str(e)}"}

    def get_market_info(self, market: str) -> Dict:
        """Get market information including price precision"""
        params = {"market": market}
        return self.send_request("/v2/market/detail", params, method="GET")

    def get_account_info(self) -> Dict:
        """Get account balance information"""
        return self.send_request("/v2/assets/spot/balance", method="GET")

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get the current price for a trading pair"""
        try:
            market_info = self.get_market_info(symbol)
            if market_info.get("code") == 0:
                return float(market_info["data"]["last"])
            return None
        except Exception as e:
            logger.error(f"Error getting current price: {str(e)}")
            return None

    def monitor_take_profit(self, symbol: str, entry_price: float, side: str, order_id: str):
        """Monitor position for take profit"""
        target_profit = 0.15  # 15% profit target
        
        while True:
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    time.sleep(1)
                    continue

                # Calculate profit percentage
                if side == "buy":
                    profit_pct = (current_price - entry_price) / entry_price
                else:  # sell (short)
                    profit_pct = (entry_price - current_price) / entry_price

                logger.info(f"Current profit for {symbol}: {profit_pct:.2%}")

                # Check if take profit is reached
                if profit_pct >= target_profit:
                    logger.info(f"🎯 Take profit reached for {symbol}! Closing position...")
                    
                    # Close position with market order
                    close_side = "sell" if side == "buy" else "buy"
                    params = {
                        "market": symbol.upper(),
                        "type": "market",
                        "side": close_side
                    }
                    
                    response = self.send_request("/v2/spot/order", params, method="POST")
                    if response.get("code") == 0:
                        logger.info(f"✅ Position closed successfully at {profit_pct:.2%} profit")
                    else:
                        logger.error(f"❌ Failed to close position: {response}")
                    
                    # Remove from active trades
                    if symbol in self.active_trades:
                        del self.active_trades[symbol]
                    break

            except Exception as e:
                logger.error(f"Error monitoring take profit: {str(e)}")
            
            time.sleep(1)  # Check every second

    def calculate_position_size(self, symbol: str, price: float, leverage: int) -> float:
        """Calculate position size based on account balance and risk management"""
        account_info = self.get_account_info()
        if account_info.get("code") == 0:
            # Get USDT balance
            usdt_balance = float(account_info["data"]["USDT"]["available"])
            # Use 5% of available balance for each trade
            position_size = (usdt_balance * 0.05) / price
            return round(position_size, 4)
        return 0.0

    def process_trade_signal(self, signal: str) -> Optional[Dict]:
        """Process trading signal and execute trade immediately"""
        try:
            # Parse signal
            match = re.search(r"(?:BYBIT:)?(ENTER-(?:LONG|SHORT))🔴-Leverage-(\d+)X👈,([\w\d]+),💲current price = ([\d.]+)", signal)
            
            if not match:
                # Try second format: "BINANCE:LONG🟢-TP3,WIFUSDT,💲current price = 0.609"
                match = re.search(r"(?:BINANCE:)?(LONG|SHORT)🟢-TP3,([\w\d]+),💲current price = ([\d.]+)", signal)
                if match:
                    # For second format
                    side = match.group(1)
                    symbol = match.group(2)
                    signal_price = float(match.group(3))
                    leverage = 10  # Default leverage for second format
                else:
                    logger.error("❌ Invalid signal format!")
                    return None
            else:
                # For first format
                order_type = match.group(1)
                leverage = int(match.group(2))
                symbol = match.group(3)
                signal_price = float(match.group(4))
                side = "LONG" if "LONG" in order_type else "SHORT"

            # Convert side to API format
            api_side = "buy" if side == "LONG" else "sell"
            
            # Get current market price
            current_price = self.get_current_price(symbol)
            if current_price is None:
                logger.error("Failed to get current price")
                return None

            logger.info(f"📌 Opening {api_side} position for {symbol} at market price")

            # Calculate position size
            amount = self.calculate_position_size(symbol, current_price, leverage)
            if amount <= 0:
                logger.error("Invalid position size calculated")
                return None

            # Place market order
            params = {
                "market": symbol.upper(),
                "type": "market",
                "amount": str(amount),
                "side": api_side
            }

            response = self.send_request("/v2/spot/order", params, method="POST")
            
            if response.get("code") == 0:
                logger.info(f"✅ Position opened successfully: {response}")
                order_id = response.get("data", {}).get("id")
                
                # Start monitoring take profit in a separate thread
                self.active_trades[symbol] = {
                    "entry_price": current_price,
                    "side": api_side,
                    "order_id": order_id
                }
                
                thread = threading.Thread(
                    target=self.monitor_take_profit,
                    args=(symbol, current_price, api_side, order_id)
                )
                thread.daemon = True
                thread.start()
                
                return response
            else:
                logger.error(f"❌ Failed to open position: {response}")
                return response

        except Exception as e:
            logger.error(f"Error processing signal: {str(e)}")
            return None

    def test_authentication(self) -> bool:
        """Test API credentials by fetching account information"""
        try:
            account_info = self.get_account_info()
            logger.info(f"Raw API Response: {account_info}")
            
            if account_info.get("code") == 0:
                logger.info("✅ Successfully authenticated with CoinEx!")
                if "data" in account_info:
                    for balance in account_info["data"]:
                        if float(balance.get("available", 0)) > 0:
                            logger.info(f"Balance for {balance['ccy']}: {balance['available']} (Available)")
                return True
            else:
                logger.error(f"❌ Authentication failed: {account_info.get('message', 'Unknown error')}")
                return False
        except Exception as e:
            logger.error(f"❌ Authentication test failed: {str(e)}")
            return False 