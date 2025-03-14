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
# from coinex_bot import CoinexTradingBot

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
APIKEY, APISECRET, FORMAT_NAME, FORMAT_PATTERN, FORMAT_EXAMPLE, SIGNAL = range(6)

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
        self.active_trades = {}  # Store active trades
        
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


    def get_account_info(self) -> Dict:
        """Get account balance information"""
        return self.send_request("/v2/assets/spot/balance", method="GET")  # Updated to correct endpoint

    def calculate_position_size(self, symbol: str, price: float, leverage: int) -> float:
        """Calculate position size based on account balance and risk management"""
        account_info = self.get_account_info()

        if account_info.get("code") != 0:
            return 0.0
            # Get USDT balance
        if account_info["data"] == None:
            return 0.0

            
        usdt_balance = float([balance for balance in account_info["data"] if balance["ccy"] == "USDT"][0]["available"])

        # Use 5% of available balance for each trade
        position_size = (usdt_balance * 0.05) / price

        return round(position_size, 4)
            

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol"""
        try:
            params = {"market": symbol}
            response = self.send_request("/v2/market/ticker", params)
            if response.get("code") == 0:
                return float(response["data"]["ticker"]["last"])
            return None
        except Exception as e:
            logger.error(f"Error getting current price: {str(e)}")
            return None

    def process_trade_signal(self, signal: str) -> Optional[Dict]:
        """Process trading signal and execute trade"""
        try:
            # Updated regex to handle the format: "BYBIT:ENTER-SHORT🔴-Leverage-10X👈,MNTUSDT,💲current price = 0.9478"
            # Get user's signal formats from database
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('SELECT pattern FROM signal_formats')
            formats = c.fetchall()
            conn.close()

            # Try each format pattern until we find a match
            match = None
            for format_pattern in formats:
                pattern = format_pattern[0]
                match = re.search(pattern, signal)
                if match:
                    break

            if not match:
                logger.error("❌ Signal doesn't match any known format!")
                return None
            
            order_type = match.group(1)
            leverage = int(match.group(2)) if match.group(2) else 10
            symbol = match.group(3)
            price = float(match.group(4))

            logger.info(f"📌 Trade detected: {order_type} | {symbol} | Leverage: {leverage}X | Price: {price}")

            # Determine order side
            side = "sell" if "SHORT" in order_type else "buy"

            # Calculate position size
            amount = self.calculate_position_size(symbol, price, leverage)
            if amount <= 0.00001:
                logger.error("Invalid position size calculated")
                return None

            # Place the order
            order_result = self.place_order(symbol, side, price, amount, leverage)
            
            if order_result and order_result.get("code") == 0:
                # Store trade information
                self.active_trades[symbol] = {
                    "side": side,
                    "entry_price": price,
                    "amount": amount,
                    "leverage": leverage,
                    "order_id": order_result["data"]["order_id"]
                }
                logger.info(f"✅ Trade stored: {symbol} - {side} at {price}")
            
            return order_result

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
        self.trading_enabled: Dict[int, bool] = {}  # Track trading status per user
        setup_database()
        self.load_signal_formats()
    # for command /start
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

    # for command /start
    async def start(self, update: Update, context: CallbackContext) -> int:
        """Start the conversation and ask for API key."""
        user_id = update.effective_user.id
        await update.message.reply_text(
            "Welcome to the CoinEx Trading Bot! 🤖\n"
            "Please provide your CoinEx API Key:"
        )
        return APIKEY

    # for command /start
    async def api_key(self, update: Update, context: CallbackContext) -> int:
        """Store API key and ask for API secret."""
        context.user_data['api_key'] = update.message.text
        await update.message.reply_text("Great! Now please provide your API Secret:")
        return APISECRET

    # for command /start
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
            "/addformat - Add new signal format\n"
            "/formats - List your signal formats\n"
            "/deleteformat - Delete a signal format\n"
            "/starttrade - Start trading\n"
            "/status - Check your active trades\n"
            "/balance - Check your account balance"
        )
        return ConversationHandler.END

    # for command /cancel
    async def cancel(self, update: Update, context: CallbackContext) -> int:
        """Cancel the conversation."""
        await update.message.reply_text("Setup cancelled. Use /start to try again.")
        return ConversationHandler.END

    # for command /balance
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
            if account_info["data"] != None:
                for balance_data in account_info["data"]:
                    if float(balance_data["available"]) > 0:
                        balances.append(f"{balance_data['ccy']}: {balance_data['available']}")
                if balances:
                    await update.message.reply_text("Your balances:\n" + "\n".join(balances))
                else:
                    await update.message.reply_text("No available balance found.")
            else:
                await update.message.reply_text("No available balance found.")
        else:
            await update.message.reply_text("Failed to fetch balance. Please check your API credentials.")

    # for command /start
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

    # for command /addformat
    async def add_format(self, update: Update, context: CallbackContext) -> int:
        """Start the process of adding a new signal format."""
        await update.message.reply_text(
            "Let's add a new signal format! 🎯\n"
            "First, give this format a name (e.g., 'binance_long', 'bybit_short'):"
        )
        return FORMAT_NAME

    # for command /addformat
    async def format_name(self, update: Update, context: CallbackContext) -> int:
        """Store format name and ask for pattern."""
        context.user_data['format_name'] = update.message.text
        await update.message.reply_text(
            "Great! Now, send me an example signal message that follows this format.\n"
            "For example:\n"
            "BINANCE:LONG🟢-TP3,WIFUSDT,💲current price = 0.609"
        )
        return FORMAT_PATTERN

    # for command /addformat
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

    # for command /addformat
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
    
    # for command /formats
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

    # for command /deleteformat
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

    # for command /starttrade
    async def start_trading(self, update: Update, context: CallbackContext):
        """Enable trading for the user."""
        user_id = update.effective_user.id
        bot = self.get_user_bot(user_id)

        if not bot:
            await update.message.reply_text("Please set up your API credentials first using /start")
            return

        self.trading_enabled[user_id] = True
        await update.message.reply_text(
            "✅ Trading enabled! please send signal to the bot"
        )
        return SIGNAL

    # for command /start
    async def handle_channel_message(self, update: Update, context: CallbackContext):
        """Process messages from the monitored channel."""
        user_id = update.effective_user.id
        bot = self.get_user_bot(user_id)
        if not bot:
            await update.message.reply_text("Please set up your API credentials first using /start")
            return

        message = update.message.text
        # Process the signal
        await self.process_signal(bot, message, user_id, context)

    # for command /start
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

    # for command /status
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

    # for command /commands
    async def show_commands(self, update: Update, context: CallbackContext):
        """Show available commands."""
        await update.message.reply_text(
            "Available commands:\n"
            "/addformat - Add new signal format\n"
            "/formats - List your signal formats\n"
            "/deleteformat - Delete a signal format\n"
            "/starttrade - Start trading\n"
            "/status - Check your active trades\n"
            "/balance - Check your account balance"
        )


def main():
    # Load your bot token from environment variable
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error("Please set TELEGRAM_BOT_TOKEN environment variable")
        return

    trading_bot = TradingBot()
    application = Application.builder().token(bot_token).build()

    application.add_handler(CommandHandler('commands', trading_bot.show_commands))
    
    # Add handlers
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', trading_bot.start)],
        states={
            APIKEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.api_key)],
            APISECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.api_secret)],
        },
        fallbacks=[CommandHandler('cancel', trading_bot.cancel)]
    ))

    # Add trading control handlers
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler('starttrade', trading_bot.start_trading)],
        states={
            SIGNAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, trading_bot.handle_channel_message)],
        },
        fallbacks=[CommandHandler('cancel', trading_bot.cancel)]
    ))

    # Add existing handlers
    application.add_handler(CommandHandler('balance', trading_bot.check_balance))
    application.add_handler(CommandHandler('status', trading_bot.status))
    # application.add_handler(MessageHandler(filters.ChatType.CHANNEL, trading_bot.handle_channel_message))

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
