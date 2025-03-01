import requests
import hmac
import hashlib
import time
import json
import re
from typing import Dict, Optional, Union
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# API_KEY = "7EF984E166E14A689952380DEF1574DF" # Access ID
API_KEY = "95BC35C5E29F40D88D1416F3952E7295" # Access ID
# API_SECRET = "E9A1B497ACFCB0DEB04561C2C3926546C819FFC1E8F50562" # Secret Key
API_SECRET = "39EB6347FB5BB1DBED47F6F6CC288452FBCC2D3E516424E9" # Secret Key
BASE_URL = "https://api.coinex.com"

class RateLimitError(Exception):
    pass

class CoinexTradingBot:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = BASE_URL
        
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
        return self.send_request("/v2/assets/spot/balance", method="GET")  # Updated to correct endpoint

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
        """Process trading signal and execute trade"""
        try:
            # Updated regex to handle the format: "BYBIT:ENTER-SHORT🔴-Leverage-10X👈,MNTUSDT,💲current price = 0.9478"
            match = re.search(r"(?:BYBIT:)?(ENTER-(?:LONG|SHORT))🔴-Leverage-(\d+)X👈,([\w\d]+),💲current price = ([\d.]+)", signal)
            
            if not match:
                logger.error("❌ Invalid signal format!")
                return None
            
            order_type = match.group(1)
            leverage = int(match.group(2))
            symbol = match.group(3)
            price = float(match.group(4))

            logger.info(f"📌 Trade detected: {order_type} | {symbol} | Leverage: {leverage}X | Price: {price}")

            # Determine order side
            side = "sell" if "SHORT" in order_type else "buy"
            
            # Get market information
            market_info = self.get_market_info(symbol)
            if market_info.get("code") != 0:
                logger.error(f"Failed to get market info: {market_info}")
                return None

            # Calculate position size
            amount = self.calculate_position_size(symbol, price, leverage)
            if amount <= 0:
                logger.error("Invalid position size calculated")
                return None

            return self.place_order(symbol, side, price, amount, leverage)

        except Exception as e:
            logger.error(f"Error processing signal: {str(e)}")
            return None

    def place_order(self, symbol: str, side: str, price: float, amount: float, leverage: int) -> Dict:
        """Place a limit order"""
        try:
            params = {
                "market": symbol.upper(),
                "type": "limit",
                "amount": str(amount),
                "price": str(price),
                "side": side
            }

            response = self.send_request("/v2/spot/order", params, method="POST")  # Updated to correct endpoint
            if response.get("code") == 0:
                logger.info(f"✅ Order placed successfully: {response}")
            else:
                logger.error(f"❌ Error placing order: {response}")
            return response

        except Exception as e:
            logger.error(f"Error placing order: {str(e)}")
            return {"code": -1, "message": str(e)}

    def test_authentication(self) -> bool:
        """Test API credentials by fetching account information"""
        try:
            account_info = self.get_account_info()
            logger.info(f"Raw API Response: {account_info}")  # Debug log
            
            if account_info.get("code") == 0:
                logger.info("✅ Successfully authenticated with CoinEx!")
                # Display available balance
                logger.info(account_info, ' account info')
                if "data" in account_info:
                    for currency, details in account_info["data"].items():
                        if float(details.get("available", 0)) > 0:
                            logger.info(f"Balance for {currency}: {details['available']} (Available)")
                return True
            else:
                logger.error(f"❌ Authentication failed: {account_info.get('message', 'Unknown error')}")
                return False
        except Exception as e:
            logger.error(f"❌ Authentication test failed: {str(e)}")
            return False

def main():
    # Initialize the trading bot
    bot = CoinexTradingBot(API_KEY, API_SECRET)
    
    # Test authentication first
    if not bot.test_authentication():
        logger.error("Failed to authenticate with CoinEx. Please check your API credentials.")
        return

    # Example signal
    signal_message = "ENTER-LONG🔴-Leverage-20X👈,BTCUSDT,💲current price = 45000.50"
    
    # Process the signal
    # result = bot.process_trade_signal(signal_message)
    
    # if result and result.get("code") == 0:
    #     logger.info("Trade executed successfully!")
    # else:
    #     logger.error("Failed to execute trade")

if __name__ == "__main__":
    main()
