# ============================================================
#  MÓDULO: executor.py
#  Gestión de órdenes en Binance Testnet (REST API)
# ============================================================

import time
import hmac
import hashlib
import requests
from logger import log, log_trade, alert_trade, alert_close
from config import (
    API_KEY, API_SECRET, BASE_URL,
    TRADE_AMOUNT_USDT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    MAX_OPEN_POSITIONS,
)


# ============================================================
#  Estado global de posiciones abiertas
#  { symbol: { "side": "BUY", "entry_price": float,
#              "quantity": float, "stop_loss": float,
#              "take_profit": float } }
# ============================================================
open_positions: dict = {}


# --- Helpers REST ------------------------------------------
def _sign(params: dict) -> str:
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}

def _timestamp() -> int:
    return int(time.time() * 1000)


def get_symbol_info(symbol: str) -> dict | None:
    """Retorna info de filtros del par (LOT_SIZE, MIN_NOTIONAL)."""
    url = f"{BASE_URL}/api/v3/exchangeInfo"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
    except Exception as e:
        log.error(f"exchangeInfo error: {e}")
    return None


def _round_quantity(symbol: str, raw_qty: float) -> float | None:
    """Redondea cantidad al step size del par."""
    info = get_symbol_info(symbol)
    if not info:
        return None
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            precision = len(str(step).rstrip("0").split(".")[-1])
            qty = round(raw_qty - (raw_qty % step), precision)
            return qty
    return round(raw_qty, 6)


def get_balance(asset: str = "USDT") -> float:
    """Consulta balance de un asset."""
    params = {"timestamp": _timestamp()}
    params["signature"] = _sign(params)
    try:
        r = requests.get(
            f"{BASE_URL}/api/v3/account",
            headers=_headers(), params=params, timeout=10
        )
        data = r.json()
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    except Exception as e:
        log.error(f"get_balance error: {e}")
    return 0.0


def place_market_order(symbol: str, side: str, quantity: float) -> dict | None:
    """Envía orden MARKET. side: 'BUY' o 'SELL'."""
    params = {
        "symbol":    symbol,
        "side":      side,
        "type":      "MARKET",
        "quantity":  quantity,
        "timestamp": _timestamp(),
    }
    params["signature"] = _sign(params)
    try:
        r = requests.post(
            f"{BASE_URL}/api/v3/order",
            headers=_headers(), params=params, timeout=10
        )
        data = r.json()
        if "orderId" in data:
            log.debug(f"Orden ejecutada: {data}")
            return data
        else:
            log.error(f"Error orden {symbol} {side}: {data}")
            return None
    except Exception as e:
        log.error(f"place_market_order exception: {e}")
        return None


# --- Lógica principal de entrada/salida --------------------

def try_open_position(symbol: str, action: str, price: float,
                      confirmations: int, indicators: dict) -> bool:
    """
    Intenta abrir posición si:
      - No hay posición abierta en ese símbolo
      - No se alcanzó MAX_OPEN_POSITIONS
      - Hay USDT suficiente
    """
    if symbol in open_positions:
        return False
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        log.debug(f"Max posiciones abiertas ({MAX_OPEN_POSITIONS}), ignorando {symbol}")
        return False

    usdt_balance = get_balance("USDT")
    if usdt_balance < TRADE_AMOUNT_USDT:
        log.warning(f"Balance insuficiente: {usdt_balance:.2f} USDT")
        return False

    raw_qty = TRADE_AMOUNT_USDT / price
    quantity = _round_quantity(symbol, raw_qty)
    if not quantity or quantity <= 0:
        log.error(f"Cantidad inválida para {symbol}: {raw_qty}")
        return False

    # Solo operamos BUY en Spot (no hay short sin margen)
    if action != "BUY":
        return False

    result = place_market_order(symbol, "BUY", quantity)
    if not result:
        return False

    # Precio de ejecución real (puede diferir levemente)
    exec_price = float(result.get("fills", [{}])[0].get("price", price)) if result.get("fills") else price

    stop_loss   = exec_price * (1 - STOP_LOSS_PCT)
    take_profit = exec_price * (1 + TAKE_PROFIT_PCT)

    open_positions[symbol] = {
        "side":         "BUY",
        "entry_price":  exec_price,
        "quantity":     quantity,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
    }

    log_trade(symbol, "BUY", exec_price, TRADE_AMOUNT_USDT,
              quantity, stop_loss, take_profit, confirmations, indicators)
    alert_trade(symbol, "BUY", exec_price, stop_loss, take_profit, confirmations)
    return True


def check_exit_conditions(symbol: str, current_price: float, high_price: float = None) -> bool:
    """
    Verifica SL/TP para posición abierta.
    Retorna True si cerró la posición.
    """
    if symbol not in open_positions:
        return False

    pos = open_positions[symbol]

    # TP: verificar con el MÁXIMO de la vela (no solo el cierre)
    # Así capturamos el TP aunque el precio haya tocado y rebotado
    check_price_tp = high_price if high_price else current_price
    hit_tp = check_price_tp >= pos["take_profit"]
    hit_sl = current_price  <= pos["stop_loss"]

    if not (hit_tp or hit_sl):
        return False

    reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"

    result = place_market_order(symbol, "SELL", pos["quantity"])
    if not result:
        log.error(f"Error cerrando posición {symbol}")
        return False

    exec_price = float(result.get("fills", [{}])[0].get("price", current_price)) if result.get("fills") else current_price
    pnl_pct = ((exec_price - pos["entry_price"]) / pos["entry_price"]) * 100

    log_trade(symbol, f"SELL_{reason}", exec_price, 0,
              pos["quantity"], pos["stop_loss"], pos["take_profit"], 0, {})
    alert_close(symbol, reason, pnl_pct)

    del open_positions[symbol]
    log.info(f"Posición cerrada {symbol} | Razón: {reason} | P&L: {pnl_pct:+.2f}%")
    return True


def force_close_all():
    """Cierra todas las posiciones abiertas (para shutdown limpio)."""
    for symbol in list(open_positions.keys()):
        pos = open_positions[symbol]
        log.warning(f"Force close {symbol}")
        place_market_order(symbol, "SELL", pos["quantity"])
        del open_positions[symbol]
