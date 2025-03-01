import requests
import hmac
import hashlib
import time
import json
import re

API_KEY = "7EF984E166E14A689952380DEF1574DF" # Access ID
API_SECRET = "E9A1B497ACFCB0DEB04561C2C3926546C819FFC1E8F50562" # Secret Key
BASE_URL = "https://api.coinex.com/v2"

def create_signature(params):
    sorted_params = sorted(params.items())
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    signature = hmac.new(API_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return signature

def send_request(endpoint, params):
    params["access_id"] = API_KEY
    params["tonce"] = int(time.time() * 1000)
    params["signature"] = create_signature(params)
    
    url = f"{BASE_URL}{endpoint}"
    response = requests.post(url, data=params)
    return response.json()

def process_trade_signal(signal):
    match = re.search(r"(ENTER-(LONG|SHORT))🔴-Leverage-(\d+)X👈,([\w\d]+),💲current price = ([\d.]+)", signal)
    if not match:
        print("❌ Invalid signal format!")
        return None
    
    order_type = match.group(1)
    leverage = int(match.group(3)) 
    symbol = match.group(4)
    price = float(match.group(5)) 

    print(f"📌 Trade detected: {order_type} | {symbol} | Leverage: {leverage}X | Price: {price}")

    side = "sell" if "SHORT" in order_type else "buy"  # نوع سفارش

    return place_order(symbol, side, price, leverage)

def place_order(symbol, side, price, leverage):
    params = {
        "market": symbol.upper(),
        "type": "limit",
        "amount": "10",
        "price": price,
        "side": side
    }

    response = send_request("/order/limit", params)
    if response.get("code") == 0:
        print("✅ Order placed:", response)
    else:
        print("❌ Error placing order:", response)

signal_message = "BYBIT:ENTER-SHORT🔴-Leverage-10X👈,MNTUSDT,💲current price = 0.9478"

process_trade_signal(signal_message)
