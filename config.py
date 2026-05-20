# ============================================================
#  SCALPING BOT — config.py  v3
#  Variables sensibles desde ENV (Railway)
# ============================================================

import os

# --- API KEYS ----------------------------------------------
API_KEY    = os.getenv("BINANCE_API_KEY",    "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# --- MODO --------------------------------------------------
TESTNET  = False
BASE_URL = "https://api.binance.com"

# --- PARES A MONITOREAR (40 pares) -------------------------
SYMBOLS = [
    # Tier 1 — volumen máximo, spread mínimo
    "BTCUSDT",  "ETHUSDT",  "SOLUSDT",  "XRPUSDT",   "BNBUSDT",
    "DOGEUSDT", "ADAUSDT",  "TRXUSDT",  "AVAXUSDT",  "SUIUSDT",
    # Tier 2 — alto volumen, buena volatilidad
    "LINKUSDT", "DOTUSDT",  "LTCUSDT",  "UNIUSDT",   "ATOMUSDT",
    "NEARUSDT", "AAVEUSDT", "APTUSDT",  "INJUSDT",   "OPUSDT",
    "ARBUSDT",  "WIFUSDT",  "FETUSDT",  "TIAUSDT",   "SEIUSDT",
    # Tier 3 — volumen suficiente, mayor volatilidad
    "FILUSDT",  "SANDUSDT", "MANAUSDT", "RUNEUSDT",  "LDOUSDT",
    "JUPUSDT",  "ENAUSDT",  "WLDUSDT",  "PENDLEUSDT","APEUSDT",
    "RENDERUSDT","POLUSDT", "EIGENUSDT","NOTUSDT",   "MKRUSDT",
]

# --- TIMEFRAME ---------------------------------------------
KLINE_INTERVAL = "1m"
KLINE_BUFFER   = 100

# --- INDICADORES -------------------------------------------
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35       # BUY obligatorio si RSI < 35
RSI_OVERBOUGHT = 65       # SELL obligatorio si RSI > 65
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
EMA_FAST       = 9
EMA_SLOW       = 21
BB_PERIOD      = 20
BB_STD         = 2.0

# --- LÓGICA DE SEÑAL ---------------------------------------
# "rsi_plus_one" → RSI obligatorio + al menos 1 confirmación (MACD/EMA/BB)
# "any_three"    → cualquier 3 de 4 indicadores (modo legacy)
SIGNAL_MODE = "rsi_plus_one"

# --- GESTIÓN DE RIESGO ------------------------------------
TRADE_AMOUNT_USDT  = float(os.getenv("TRADE_AMOUNT_USDT", "50.0"))
STOP_LOSS_PCT      = 0.015   # -1.5% fijo
TAKE_PROFIT_PCT    = 0.005   # +0.5% activa trailing stop  ← CAMBIADO de 0.008
MAX_OPEN_POSITIONS = 5       # ← CAMBIADO de 3

# --- TELEGRAM ---------------------------------------------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- LOGS -------------------------------------------------
LOG_FILE  = "trades.csv"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# --- DASHBOARD --------------------------------------------
DASHBOARD_PORT = int(os.getenv("PORT", "5000"))
