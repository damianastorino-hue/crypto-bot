# ============================================================
#  MÓDULO: logger.py
#  Registro de operaciones en CSV + alertas Telegram
# ============================================================

import csv
import logging
import os
import requests
from datetime import datetime
from config import LOG_FILE, LOG_LEVEL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID


# --- Logger estándar Python --------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ScalpBot")


# --- CSV ---------------------------------------------------
CSV_HEADERS = [
    "timestamp", "symbol", "action", "price",
    "amount_usdt", "quantity", "stop_loss",
    "take_profit", "confirmations", "indicators"
]

def _ensure_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()

def log_trade(symbol: str, action: str, price: float, amount_usdt: float,
              quantity: float, stop_loss: float, take_profit: float,
              confirmations: int, indicators: dict):
    """Registra una operación en el CSV."""
    _ensure_csv()
    row = {
        "timestamp":     datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":        symbol,
        "action":        action,
        "price":         round(price, 6),
        "amount_usdt":   round(amount_usdt, 2),
        "quantity":      round(quantity, 6),
        "stop_loss":     round(stop_loss, 6),
        "take_profit":   round(take_profit, 6),
        "confirmations": confirmations,
        "indicators":    str(indicators),
    }
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writerow(row)
    log.info(f"[{action}] {symbol} @ {price:.4f} | SL:{stop_loss:.4f} TP:{take_profit:.4f} | conf:{confirmations}/4")


# --- Telegram ----------------------------------------------
def send_telegram(message: str):
    """Envía alerta a Telegram. No lanza excepción si falla."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram error: {e}")


def alert_trade(symbol: str, action: str, price: float,
                stop_loss: float, take_profit: float, confirmations: int):
    emoji = "🟢" if action == "BUY" else "🔴"
    msg = (
        f"{emoji} <b>{action} {symbol}</b>\n"
        f"Precio: <code>{price:.6f}</code>\n"
        f"SL: <code>{stop_loss:.6f}</code> | TP: <code>{take_profit:.6f}</code>\n"
        f"Confirmaciones: {confirmations}/4"
    )
    send_telegram(msg)


def alert_close(symbol: str, reason: str, pnl_pct: float):
    emoji = "✅" if pnl_pct >= 0 else "❌"
    msg = (
        f"{emoji} <b>CIERRE {symbol}</b>\n"
        f"Razón: {reason}\n"
        f"P&L: <code>{pnl_pct:+.2f}%</code>"
    )
    send_telegram(msg)
