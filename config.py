# ============================================================
#  SCALPING BOT - CONFIG
#  Variables sensibles desde ENV (Railway)
# ============================================================

import os

# --- API KEYS (desde variables de entorno en Railway) ------
API_KEY    = os.getenv("BINANCE_API_KEY",    "TU_API_KEY_TESTNET")
API_SECRET = os.getenv("BINANCE_API_SECRET", "TU_API_SECRET_TESTNET")

# --- MODO --------------------------------------------------
TESTNET  = False
BASE_URL = "https://api.binance.com"

# --- PARES A MONITOREAR ------------------------------------
SYMBOLS = [
    # TIER 1
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
    "DOGEUSDT", "ADAUSDT", "TRXUSDT", "AVAXUSDT", "SUIUSDT",
    # TIER 2
    "LINKUSDT", "DOTUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT",
    "NEARUSDT", "AAVEUSDT", "APTUSDT", "INJUSDT", "OPUSDT",
    "ARBUSDT", "WIFUSDT", "FETUSDT", "TIAUSDT", "SEIUSDT",
    # TIER 3
    "FILUSDT", "SANDUSDT", "MANAUSDT", "RUNEUSDT", "LDOUSDT",
    "JUPUSDT", "ENAUSDT", "WLDUSDT", "PENDLEUSDT", "APEUSDT",
    "RENDERUSDT", "POLUSDT", "EIGENUSDT", "NOTUSDT", "MKRUSDT",
]

# --- TIMEFRAME ---------------------------------------------
KLINE_INTERVAL = "1m"
KLINE_BUFFER   = 100

# --- INDICADORES -------------------------------------------
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
EMA_FAST       = 9
EMA_SLOW       = 21
BB_PERIOD      = 20
BB_STD         = 2.0

# --- SEÑAL -------------------------------------------------
MIN_CONFIRMATIONS = 3

# --- GESTIÓN DE RIESGO ------------------------------------
TRADE_AMOUNT_USDT  = float(os.getenv("TRADE_AMOUNT_USDT", "50.0"))
STOP_LOSS_PCT      = 0.015
TAKE_PROFIT_PCT    = 0.025
MAX_OPEN_POSITIONS = 3

# --- TELEGRAM (opcional) ----------------------------------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- LOGS -------------------------------------------------
LOG_FILE  = "trades.csv"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- DASHBOARD --------------------------------------------
DASHBOARD_PORT = int(os.getenv("PORT", "5000"))  # Railway inyecta PORT automáticamente
