# ============================================================
#  SCALPING BOT — logger.py  v3
#  CSV + Telegram + Google Sheets
# ============================================================

import csv
import json
import logging
import os
import threading
import requests
from datetime import datetime, timezone, timedelta
from config import LOG_FILE, LOG_LEVEL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# --- Logger estándar Python --------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ScalpBot")

# --- CSV ---------------------------------------------------
CSV_HEADERS = [
    "timestamp", "symbol", "action", "price",
    "amount_usdt", "quantity", "stop_loss",
    "take_profit", "confirmations", "indicators",
]

def _ensure_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()


def log_trade(symbol: str, action: str, price: float, amount_usdt: float,
              quantity: float, stop_loss: float, take_profit: float,
              confirmations: int, indicators: dict):
    _ensure_csv()
    row = {
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
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
        csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
    log.info(
        f"[{action}] {symbol} @ {price:.4f} | "
        f"SL:{stop_loss:.4f} TP:{take_profit:.4f} | conf:{confirmations}"
    )


# ============================================================
#  Google Sheets
# ============================================================
_sheets_client    = None
_sheets_lock      = threading.Lock()
_SPREADSHEET_NAME = os.getenv("SHEETS_NAME", "ScalpingBot_Trades")
_WORKSHEET_TRADES = "Trades"

SHEETS_HEADERS = [
    "Fecha (ARG)", "Hora (ARG)", "Par", "Accion",
    "Precio Entrada", "Precio Salida", "Cantidad",
    "USDT Invertido", "Ganancia USD", "PnL %",
    "Stop Loss", "Take Profit", "Razon", "Confirmaciones",
]


def _get_sheets_client():
    global _sheets_client
    if _sheets_client is not None:
        return _sheets_client
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if not creds_json:
        log.warning("GOOGLE_CREDENTIALS_JSON no configurado — Sheets deshabilitado")
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
        _sheets_client = gspread.authorize(creds)
        log.info("✅ Google Sheets conectado")
        return _sheets_client
    except Exception as e:
        log.warning(f"Google Sheets init error: {e}")
        return None


def _get_or_create_worksheet(spreadsheet, name: str, headers: list):
    try:
        return spreadsheet.worksheet(name)
    except Exception:
        ws = spreadsheet.add_worksheet(title=name, rows=5000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        try:
            ws.format(f"A1:{chr(64+len(headers))}1", {
                "textFormat":          {"bold": True},
                "backgroundColor":     {"red": 0.13, "green": 0.13, "blue": 0.13},
                "horizontalAlignment": "CENTER",
            })
        except Exception:
            pass
        return ws


def _open_spreadsheet():
    gc = _get_sheets_client()
    if not gc:
        return None
    try:
        return gc.open(_SPREADSHEET_NAME)
    except Exception:
        try:
            ss = gc.create(_SPREADSHEET_NAME)
            log.info(f"Planilla '{_SPREADSHEET_NAME}' creada automaticamente")
            return ss
        except Exception as e:
            log.warning(f"No pude abrir/crear planilla: {e}")
            return None


def sheets_log_buy(symbol: str, price: float, quantity: float,
                   amount_usdt: float, stop_loss: float,
                   take_profit: float, confirmations: int):
    """Registra compra. Precio Salida y Ganancia quedan vacíos hasta el cierre."""
    def _write():
        with _sheets_lock:
            ss = _open_spreadsheet()
            if not ss:
                return
            ws = _get_or_create_worksheet(ss, _WORKSHEET_TRADES, SHEETS_HEADERS)
            now_arg = datetime.now(timezone.utc) - timedelta(hours=3)
            ws.append_row([
                now_arg.strftime("%Y-%m-%d"),
                now_arg.strftime("%H:%M:%S"),
                symbol, "BUY",
                round(price, 6), "",
                round(quantity, 6),
                round(amount_usdt, 2),
                "", "",
                round(stop_loss, 6),
                round(take_profit, 6),
                "SIGNAL", confirmations,
            ], value_input_option="USER_ENTERED")
    threading.Thread(target=_write, daemon=True).start()


def sheets_log_sell(symbol: str, entry_price: float, exit_price: float,
                    quantity: float, amount_usdt: float,
                    reason: str, pnl_pct: float):
    """
    Busca la última fila BUY abierta del símbolo y la completa.
    Si no la encuentra, agrega fila SELL nueva.
    """
    def _write():
        with _sheets_lock:
            ss = _open_spreadsheet()
            if not ss:
                return
            ws  = _get_or_create_worksheet(ss, _WORKSHEET_TRADES, SHEETS_HEADERS)
            now_arg      = datetime.now(timezone.utc) - timedelta(hours=3)
            ganancia_usd = round((exit_price - entry_price) * quantity, 4)
            try:
                rows = ws.get_all_values()
                for i in range(len(rows) - 1, 0, -1):
                    r = rows[i]
                    if len(r) >= 6 and r[2] == symbol and r[3] == "BUY" and r[5] == "":
                        n = i + 1
                        ws.update(f"D{n}", [[f"SELL_{reason}"]])
                        ws.update(f"F{n}", [[round(exit_price, 6)]])
                        ws.update(f"I{n}", [[ganancia_usd]])
                        ws.update(f"J{n}", [[round(pnl_pct, 4)]])
                        return
            except Exception as e:
                log.warning(f"Sheets update fila BUY {symbol}: {e}")
            # Fallback: fila nueva
            ws.append_row([
                now_arg.strftime("%Y-%m-%d"),
                now_arg.strftime("%H:%M:%S"),
                symbol, f"SELL_{reason}",
                round(entry_price, 6), round(exit_price, 6),
                round(quantity, 6), round(amount_usdt, 2),
                ganancia_usd, round(pnl_pct, 4),
                "", "", reason, "",
            ], value_input_option="USER_ENTERED")
    threading.Thread(target=_write, daemon=True).start()


# ============================================================
#  Telegram
# ============================================================
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")


def alert_trade(symbol: str, action: str, price: float,
                stop_loss: float, take_profit: float, confirmations: int):
    emoji = "🟢" if action == "BUY" else "🔴"
    send_telegram(
        f"{emoji} <b>{action} {symbol}</b>\n"
        f"Precio: <code>{price:.6f}</code>\n"
        f"SL: <code>{stop_loss:.6f}</code>  TP: <code>{take_profit:.6f}</code>\n"
        f"Confirmaciones: {confirmations}"
    )


def alert_close(symbol: str, reason: str, pnl_pct: float, ganancia_usd: float = 0.0):
    emoji = "✅" if pnl_pct >= 0 else "❌"
    send_telegram(
        f"{emoji} <b>CIERRE {symbol}</b>\n"
        f"Razon: {reason}\n"
        f"P&L: <code>{pnl_pct:+.2f}%</code>  (<code>${ganancia_usd:+.4f}</code>)"
    )
