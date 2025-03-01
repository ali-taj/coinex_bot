import requests
import hmac
import hashlib
import time
import json
import re
from typing import Dict, Optional, Union
import logging
from datetime import datetime
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters, ConversationHandler
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import sqlite3
from coinex_bot import CoinexTradingBot

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# API_KEY = "7EF984E166E14A689952380DEF1574DF" # Access ID
API_KEY = "95BC35C5E29F40D88D1416F3952E7295" # Access ID
# API_SECRET = "E9A1B497ACFCB0DEB04561C2C3926546C819FFC1E8F50562" # Secret Key
API_SECRET = "39EB6347FB5BB1DBED47F6F6CC288452FBCC2D3E516424E9" # Secret Key
BASE_URL = "https://api.coinex.com"

# States for conversation handler
APIKEY, APISECRET, FORMAT_NAME, FORMAT_PATTERN, FORMAT_EXAMPLE = range(5)

# Database setup
def setup_database():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            encrypted_api_key TEXT,
            encrypted_api_secret TEXT,
            active_trades TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS signal_formats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            format_name TEXT,
            pattern TEXT,
            example TEXT,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS monitored_channels
                    (user_id INTEGER, channel_id TEXT, channel_name TEXT)''')
    conn.commit()
    conn.close()

# Encryption setup
def get_encryption_key():
    key = os.getenv('ENCRYPTION_KEY')
    if not key:
        key = Fernet.generate_key()
        with open('.env', 'a') as f:
            f.write(f'\nENCRYPTION_KEY={key.decode()}')
    return key if isinstance(key, bytes) else key.encode()

# Initialize encryption
fernet = Fernet(get_encryption_key())

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

class TradingBot:
    def __init__(self):
        self.user_bots: Dict[int, CoinexTradingBot] = {}
        self.format_handlers: Dict[int, Dict[str, re.Pattern]] = {}
        self.monitored_channels: Dict[int, list] = {}  # Store monitored channels per user
        self.trading_enabled: Dict[int, bool] = {}  # Track trading status per user
        setup_database()
        self.load_signal_formats()
        self.load_monitored_channels()

    def load_signal_formats(self):
        """Load all users' signal formats from database"""
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT user_id, format_name, pattern FROM signal_formats')
        formats = c.fetchall()
        conn.close()

        for user_id, name, pattern in formats:
            if user_id not in self.format_handlers:
                self.format_handlers[user_id] = {}
            try:
                self.format_handlers[user_id][name] = re.compile(pattern)
            except re.error:
                logger.error(f"Invalid regex pattern for user {user_id}, format {name}")

    def load_monitored_channels(self):
        """Load monitored channels from database"""
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS monitored_channels
                    (user_id INTEGER, channel_id TEXT, channel_name TEXT)''')
        c.execute('SELECT user_id, channel_id, channel_name FROM monitored_channels')
        channels = c.fetchall()
        conn.close()

        for user_id, channel_id, channel_name in channels:
            if user_id not in self.monitored_channels:
                self.monitored_channels[user_id] = []
            self.monitored_channels[user_id].append((channel_id, channel_name))

    async def start(self, update: Update, context: CallbackContext) -> int:
        """Start the conversation and ask for API key."""
        user_id = update.effective_user.id
        await update.message.reply_text(
            "Welcome to the CoinEx Trading Bot! 🤖\n"
            "Please provide your CoinEx API Key:"
        )
        return APIKEY

    async def api_key(self, update: Update, context: CallbackContext) -> int:
        """Store API key and ask for API secret."""
        context.user_data['api_key'] = update.message.text
        await update.message.reply_text("Great! Now please provide your API Secret:")
        return APISECRET

    async def api_secret(self, update: Update, context: CallbackContext) -> int:
        """Store API secret and complete setup."""
        user_id = update.effective_user.id
        api_key = context.user_data['api_key']
        api_secret = update.message.text

        # Encrypt credentials
        encrypted_key = fernet.encrypt(api_key.encode())
        encrypted_secret = fernet.encrypt(api_secret.encode())

        # Save to database
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO users 
            (user_id, username, encrypted_api_key, encrypted_api_secret, active_trades) 
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, update.effective_user.username, encrypted_key, encrypted_secret, '{}'))
        conn.commit()
        conn.close()

        # Initialize trading bot for user
        self.user_bots[user_id] = CoinexTradingBot(api_key, api_secret)
        self.trading_enabled[user_id] = False  # Initialize trading as disabled

        await update.message.reply_text(
            "✅ Setup complete! Your credentials have been securely saved.\n\n"
            "Available commands:\n"
            "/addchannel - Add a channel to monitor\n"
            "/channels - List monitored channels\n"
            "/removechannel - Remove a channel\n"
            "/addformat - Add new signal format\n"
            "/formats - List your signal formats\n"
            "/deleteformat - Delete a signal format\n"
            "/starttrade - Start trading\n"
            "/stoptrade - Stop trading\n"
            "/status - Check your active trades\n"
            "/balance - Check your account balance"
        )
        return ConversationHandler.END

    async def cancel(self, update: Update, context: CallbackContext) -> int:
        """Cancel the conversation."""
        await update.message.reply_text("Setup cancelled. Use /start to try again.")
        return ConversationHandler.END

    async def check_balance(self, update: Update, context: CallbackContext):
        """Check user's account balance."""
        user_id = update.effective_user.id
        bot = self.get_user_bot(user_id)
        
        if not bot:
            await update.message.reply_text("Please set up your API credentials first using /start")
            return

        account_info = bot.get_account_info()
        if account_info.get("code") == 0:
            balances = []
            for balance_data in account_info["data"]:
                if float(balance_data["available"]) > 0:
                    balances.append(f"{balance_data['ccy']}: {balance_data['available']}")
            if balances:
                await update.message.reply_text("Your balances:\n" + "\n".join(balances))
            else:
                await update.message.reply_text("No available balance found.")
        else:
            await update.message.reply_text("Failed to fetch balance. Please check your API credentials.")

    def get_user_bot(self, user_id: int) -> Optional[CoinexTradingBot]:
        """Get or create trading bot instance for user."""
        if user_id not in self.user_bots:
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('SELECT encrypted_api_key, encrypted_api_secret FROM users WHERE user_id = ?', (user_id,))
            result = c.fetchone()
            conn.close()

            if result:
                api_key = fernet.decrypt(result[0]).decode()
                api_secret = fernet.decrypt(result[1]).decode()
                self.user_bots[user_id] = CoinexTradingBot(api_key, api_secret)

        return self.user_bots.get(user_id)

    async def add_format(self, update: Update, context: CallbackContext) -> int:
        """Start the process of adding a new signal format."""
        await update.message.reply_text(
            "Let's add a new signal format! 🎯\n"
            "First, give this format a name (e.g., 'binance_long', 'bybit_short'):"
        )
        return FORMAT_NAME

    async def format_name(self, update: Update, context: CallbackContext) -> int:
        """Store format name and ask for pattern."""
        context.user_data['format_name'] = update.message.text
        await update.message.reply_text(
            "Great! Now, send me an example signal message that follows this format.\n"
            "For example:\n"
            "BINANCE:LONG🟢-TP3,WIFUSDT,💲current price = 0.609"
        )
        return FORMAT_PATTERN

    async def format_pattern(self, update: Update, context: CallbackContext) -> int:
        """Store example and create pattern."""
        example = update.message.text
        context.user_data['example'] = example
        
        await update.message.reply_text(
            "Perfect! Now, please mark the important parts in your example using these placeholders:\n"
            "{side} - for LONG/SHORT\n"
            "{symbol} - for the trading pair\n"
            "{price} - for the price\n"
            "{leverage} - for leverage (optional)\n\n"
            "For example:\n"
            "BINANCE:{side}🟢-TP3,{symbol},💲current price = {price}"
        )
        return FORMAT_EXAMPLE

    async def format_example(self, update: Update, context: CallbackContext) -> int:
        """Save the new format."""
        pattern_template = update.message.text
        user_id = update.effective_user.id
        format_name = context.user_data['format_name']
        example = context.user_data['example']

        try:
            # Convert template to regex pattern
            pattern = (pattern_template
                .replace("{side}", "(?P<side>LONG|SHORT)")
                .replace("{symbol}", "(?P<symbol>[A-Z0-9]+)")
                .replace("{price}", "(?P<price>[0-9.]+)")
                .replace("{leverage}", "(?P<leverage>[0-9]+)")
            )

            # Test the pattern against the example
            test_pattern = re.compile(pattern)
            if not test_pattern.search(example):
                await update.message.reply_text(
                    "❌ Error: The pattern doesn't match your example message. Please try again with /addformat"
                )
                return ConversationHandler.END

            # Save to database
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('''
                INSERT INTO signal_formats (user_id, format_name, pattern, example)
                VALUES (?, ?, ?, ?)
            ''', (user_id, format_name, pattern, example))
            conn.commit()
            conn.close()

            # Add to runtime patterns
            if user_id not in self.format_handlers:
                self.format_handlers[user_id] = {}
            self.format_handlers[user_id][format_name] = test_pattern

            await update.message.reply_text(
                f"✅ Signal format '{format_name}' has been added successfully!\n"
                f"Example: {example}\n\n"
                "The bot will now recognize this format in channel messages."
            )

        except Exception as e:
            logger.error(f"Error adding format: {str(e)}")
            await update.message.reply_text(
                "❌ Error adding format. Please try again with /addformat"
            )

        return ConversationHandler.END

    async def list_formats(self, update: Update, context: CallbackContext):
        """List all signal formats for the user."""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT format_name, example FROM signal_formats WHERE user_id = ?', (user_id,))
        formats = c.fetchall()
        conn.close()

        if not formats:
            await update.message.reply_text(
                "You haven't added any custom signal formats yet.\n"
                "Use /addformat to add a new format."
            )
            return

        message = "Your signal formats:\n\n"
        for name, example in formats:
            message += f"📌 {name}:\n{example}\n\n"

        await update.message.reply_text(message)

    async def delete_format(self, update: Update, context: CallbackContext):
        """Delete a signal format."""
        user_id = update.effective_user.id
        
        if not context.args:
            await update.message.reply_text(
                "Please specify the format name to delete.\n"
                "Example: /deleteformat binance_long"
            )
            return

        format_name = context.args[0]
        
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('DELETE FROM signal_formats WHERE user_id = ? AND format_name = ?', 
                 (user_id, format_name))
        deleted = c.rowcount > 0
        conn.commit()
        conn.close()

        if deleted:
            if user_id in self.format_handlers:
                self.format_handlers[user_id].pop(format_name, None)
            await update.message.reply_text(f"✅ Format '{format_name}' has been deleted.")
        else:
            await update.message.reply_text(f"❌ Format '{format_name}' not found.")

    async def add_channel(self, update: Update, context: CallbackContext):
        """Add a channel to monitor."""
        if not context.args:
            await update.message.reply_text(
                "Please provide the channel username or ID.\n"
                "Example: /addchannel @channelname"
            )
            return

        user_id = update.effective_user.id
        channel = context.args[0]

        # Remove @ if present
        channel_name = channel.lstrip('@')

        try:
            # Try to get channel info
            chat = await context.bot.get_chat(channel)
            channel_id = str(chat.id)

            # Save to database
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('INSERT INTO monitored_channels (user_id, channel_id, channel_name) VALUES (?, ?, ?)',
                     (user_id, channel_id, channel_name))
            conn.commit()
            conn.close()

            # Add to runtime list
            if user_id not in self.monitored_channels:
                self.monitored_channels[user_id] = []
            self.monitored_channels[user_id].append((channel_id, channel_name))

            await update.message.reply_text(f"✅ Successfully added channel {channel} to monitoring list.")

        except Exception as e:
            await update.message.reply_text(
                f"❌ Failed to add channel. Make sure:\n"
                f"1. The channel exists\n"
                f"2. The bot is added to the channel\n"
                f"3. The bot is an admin in the channel"
            )

    async def list_channels(self, update: Update, context: CallbackContext):
        """List all monitored channels."""
        user_id = update.effective_user.id
        channels = self.monitored_channels.get(user_id, [])

        if not channels:
            await update.message.reply_text(
                "You haven't added any channels yet.\n"
                "Use /addchannel @channelname to add one."
            )
            return

        message = "Your monitored channels:\n\n"
        for channel_id, channel_name in channels:
            message += f"📢 @{channel_name}\n"

        await update.message.reply_text(message)

    async def remove_channel(self, update: Update, context: CallbackContext):
        """Remove a channel from monitoring."""
        if not context.args:
            await update.message.reply_text(
                "Please provide the channel username.\n"
                "Example: /removechannel @channelname"
            )
            return

        user_id = update.effective_user.id
        channel = context.args[0].lstrip('@')

        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('DELETE FROM monitored_channels WHERE user_id = ? AND channel_name = ?',
                 (user_id, channel))
        deleted = c.rowcount > 0
        conn.commit()
        conn.close()

        if deleted:
            if user_id in self.monitored_channels:
                self.monitored_channels[user_id] = [
                    (cid, cname) for cid, cname in self.monitored_channels[user_id]
                    if cname != channel
                ]
            await update.message.reply_text(f"✅ Channel @{channel} removed from monitoring.")
        else:
            await update.message.reply_text(f"❌ Channel @{channel} not found in your monitoring list.")

    async def start_trading(self, update: Update, context: CallbackContext):
        """Enable trading for the user."""
        user_id = update.effective_user.id
        if user_id not in self.user_bots:
            await update.message.reply_text("Please set up your API credentials first using /start")
            return

        self.trading_enabled[user_id] = True
        await update.message.reply_text(
            "✅ Trading enabled! The bot will now process signals from your monitored channels.\n"
            "Use /stoptrade to stop trading at any time."
        )

    async def stop_trading(self, update: Update, context: CallbackContext):
        """Disable trading for the user."""
        user_id = update.effective_user.id
        self.trading_enabled[user_id] = False
        await update.message.reply_text(
            "🛑 Trading stopped! The bot will no longer process signals.\n"
            "Use /starttrade to resume trading."
        )

    async def handle_channel_message(self, update: Update, context: CallbackContext):
        """Process messages from the monitored channel."""
        if not update.channel_post:
            return

        channel_id = str(update.channel_post.chat_id)
        message = update.channel_post.text

        if not message:
            return

        # Process message for users monitoring this channel
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT user_id FROM monitored_channels WHERE channel_id = ?', (channel_id,))
        users = c.fetchall()
        conn.close()

        for (user_id,) in users:
            # Check if trading is enabled for this user
            if not self.trading_enabled.get(user_id, False):
                continue

            bot = self.get_user_bot(user_id)
            if not bot:
                continue

            # Process the signal
            await self.process_signal(bot, message, user_id, context)

    async def process_signal(self, bot, message, user_id, context):
        """Process a signal for a specific user."""
        try:
            result = bot.process_trade_signal(message)
            if result and result.get("code") == 0:
                await context.bot.send_message(
                    user_id,
                    f"✅ Trade executed successfully for signal:\n{message}"
                )
            else:
                await context.bot.send_message(
                    user_id,
                    f"❌ Failed to execute trade for signal:\n{message}"
                )
        except Exception as e:
            await context.bot.send_message(
                user_id,
                f"❌ Error processing signal: {str(e)}"
            )

    async def status(self, update: Update, context: CallbackContext):
        """Check status of active trades."""
        user_id = update.effective_user.id
        bot = self.get_user_bot(user_id)
        
        if not bot:
            await update.message.reply_text("Please set up your API credentials first using /start")
            return

        if not bot.active_trades:
            await update.message.reply_text("No active trades.")
            return

        status_message = "Active trades:\n"
        for symbol, trade in bot.active_trades.items():
            current_price = bot.get_current_price(symbol)
            if current_price:
                if trade['side'] == "buy":
                    profit_pct = (current_price - trade['entry_price']) / trade['entry_price'] * 100
                else:
                    profit_pct = (trade['entry_price'] - current_price) / trade['entry_price'] * 100
                
                status_message += f"\n{symbol}:\n"
                status_message += f"Entry: {trade['entry_price']}\n"
                status_message += f"Current: {current_price}\n"
                status_message += f"Profit: {profit_pct:.2f}%\n"

        await update.message.reply_text(status_message)

def main():
    # Load your bot token from environment variable
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error("Please set TELEGRAM_BOT_TOKEN environment variable")
        return

    trading_bot = TradingBot()
    application = Application.builder().token(bot_token).build()

    # Add handlers
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', trading_bot.start)],
        states={
            APIKEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.api_key)],
            APISECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.api_secret)],
        },
        fallbacks=[CommandHandler('cancel', trading_bot.cancel)]
    ))

    # Add channel management handlers
    application.add_handler(CommandHandler('addchannel', trading_bot.add_channel))
    application.add_handler(CommandHandler('channels', trading_bot.list_channels))
    application.add_handler(CommandHandler('removechannel', trading_bot.remove_channel))

    # Add trading control handlers
    application.add_handler(CommandHandler('starttrade', trading_bot.start_trading))
    application.add_handler(CommandHandler('stoptrade', trading_bot.stop_trading))

    # Add existing handlers
    application.add_handler(CommandHandler('balance', trading_bot.check_balance))
    application.add_handler(CommandHandler('status', trading_bot.status))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, trading_bot.handle_channel_message))

    # Add format management handlers
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('addformat', trading_bot.add_format)],
        states={
            FORMAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.format_name)],
            FORMAT_PATTERN: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.format_pattern)],
            FORMAT_EXAMPLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.format_example)],
        },
        fallbacks=[CommandHandler('cancel', trading_bot.cancel)]
    ))
    application.add_handler(CommandHandler('formats', trading_bot.list_formats))
    application.add_handler(CommandHandler('deleteformat', trading_bot.delete_format))

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
