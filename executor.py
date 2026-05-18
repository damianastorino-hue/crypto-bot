# ============================================================
#  MÓDULO: executor.py  v4
#  Capital dinámico + trailing stop + force_sell
# ============================================================

import time
import hmac
import hashlib
import requests
from logger import log, log_trade, alert_trade, alert_close
from config import (
    API_KEY, API_SECRET, BASE_URL,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    MAX_OPEN_POSITIONS,
)

# ============================================================
#  Estado global
# ============================================================
open_positions: dict = {}
_price_precision_cache: dict = {}


# --- Helpers REST ------------------------------------------
def _sign(params: dict) -> str:
    query = "&".join([f"{k}={v}" for k, v in params.items()])
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}

def _timestamp() -> int:
    return int(time.time() * 1000)


def get_symbol_info(symbol: str) -> dict | None:
    url = f"{BASE_URL}/api/v3/exchangeInfo"
    try:
        r = requests.get(url, timeout=10)
        for s in r.json().get("symbols", []):
            if s["symbol"] == symbol:
                return s
    except Exception as e:
        log.error(f"exchangeInfo error: {e}")
    return None


def _round_quantity(symbol: str, raw_qty: float) -> float | None:
    info = get_symbol_info(symbol)
    if not info:
        return None
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            precision = len(str(step).rstrip("0").split(".")[-1])
            return round(raw_qty - (raw_qty % step), precision)
    return round(raw_qty, 6)


def get_price_precision(symbol: str) -> int:
    if symbol in _price_precision_cache:
        return _price_precision_cache[symbol]
    info = get_symbol_info(symbol)
    precision = 8
    if info:
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = f["tickSize"].rstrip("0")
                precision = len(tick.split(".")[-1]) if "." in tick else 0
                break
    _price_precision_cache[symbol] = precision
    return precision


def get_current_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"get_current_price {symbol}: {e}")
        return None


def get_balance(asset: str = "USDT") -> float:
    params = {"timestamp": _timestamp()}
    params["signature"] = _sign(params)
    try:
        r = requests.get(f"{BASE_URL}/api/v3/account",
                         headers=_headers(), params=params, timeout=10)
        for b in r.json().get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    except Exception as e:
        log.error(f"get_balance error: {e}")
    return 0.0


def get_trade_amount() -> float:
    """
    Capital dinámico: balance_disponible / posiciones_disponibles
    Siempre distribuye el capital uniformemente.
    """
    balance    = get_balance("USDT")
    available  = MAX_OPEN_POSITIONS - len(open_positions)
    if available <= 0:
        return 0.0
    amount = balance / available
    log.debug(f"Trade amount dinámico: ${balance:.2f} / {available} = ${amount:.2f}")
    return amount


def place_market_order(symbol: str, side: str, quantity: float) -> dict | None:
    params = {
        "symbol": symbol, "side": side,
        "type": "MARKET", "quantity": quantity,
        "timestamp": _timestamp(),
    }
    params["signature"] = _sign(params)
    try:
        r = requests.post(f"{BASE_URL}/api/v3/order",
                          headers=_headers(), params=params, timeout=10)
        data = r.json()
        if "orderId" in data:
            return data
        log.error(f"Error orden {symbol} {side}: {data}")
        return None
    except Exception as e:
        log.error(f"place_market_order exception: {e}")
        return None


def _close_position(symbol: str, reason: str, current_price: float) -> bool:
    """Ejecuta SELL y limpia la posición."""
    if symbol not in open_positions:
        return False
    pos    = open_positions[symbol]
    result = place_market_order(symbol, "SELL", pos["quantity"])
    if not result:
        log.error(f"Error cerrando {symbol}")
        return False

    exec_price = float(result.get("fills", [{}])[0].get("price", current_price)) \
                 if result.get("fills") else current_price
    pnl_pct = ((exec_price - pos["entry_price"]) / pos["entry_price"]) * 100

    # Guardar en DB
    try:
        from database import save_trade
        save_trade(symbol, f"SELL_{reason}", exec_price, pos["quantity"],
                   pos["quantity"] * exec_price, pos["stop_loss"],
                   pos["take_profit"], pnl_pct, reason)
    except Exception as e:
        log.warning(f"DB save_trade error: {e}")

    log_trade(symbol, f"SELL_{reason}", exec_price, 0,
              pos["quantity"], pos["stop_loss"], pos["take_profit"], 0, {})
    alert_close(symbol, reason, pnl_pct)
    del open_positions[symbol]
    log.info(f"[CERRADO] {symbol} | {reason} | P&L: {pnl_pct:+.2f}%")
    return True


# ============================================================
#  VIGILANCIA EN TIEMPO REAL — cada 15s
# ============================================================
def watch_positions() -> list[str]:
    """
    Trailing stop por momentum:
    - SL fijo → vende siempre
    - Target alcanzado + precio baja → VENDE en primer ciclo bajista
    """
    closed = []
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_current_price(symbol)
        if price is None:
            continue

        pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100

        if pnl_pct > pos.get("peak_pnl", 0):
            open_positions[symbol]["peak_pnl"] = pnl_pct

        # Stop Loss — prioridad absoluta
        if price <= pos["stop_loss"]:
            _close_position(symbol, "STOP_LOSS", price)
            closed.append(symbol)
            continue

        # Marcar target alcanzado
        if pnl_pct >= TAKE_PROFIT_PCT * 100:
            open_positions[symbol]["target_reached"] = True

        # Trailing: primer ciclo bajista después del target
        if pos.get("target_reached"):
            prev = pos.get("prev_price")
            if prev is not None and price < prev:
                log.info(f"[TRAILING] {symbol} | {prev:.6f}→{price:.6f} | P&L:{pnl_pct:+.2f}%")
                _close_position(symbol, "TRAILING_STOP", price)
                closed.append(symbol)
                continue

        open_positions[symbol]["prev_price"] = price

    return closed


# ============================================================
#  FORCE SELL — venta manual desde dashboard
# ============================================================
def force_sell(symbol: str) -> bool:
    """Venta forzada manual de una posición específica."""
    if symbol not in open_positions:
        log.warning(f"force_sell: {symbol} no está en posiciones abiertas")
        return False
    price = get_current_price(symbol) or open_positions[symbol]["entry_price"]
    log.warning(f"[FORCE SELL] {symbol} @ {price}")
    return _close_position(symbol, "FORCE_SELL", price)


def force_close_all() -> int:
    """Cierra todas las posiciones. Retorna cuántas cerró."""
    count = 0
    for symbol in list(open_positions.keys()):
        price = get_current_price(symbol) or open_positions[symbol]["entry_price"]
        if _close_position(symbol, "FORCE_SELL_ALL", price):
            count += 1
    return count


# ============================================================
#  ENTRADA
# ============================================================
def try_open_position(symbol: str, action: str, price: float,
                      confirmations: int, indicators: dict) -> bool:
    if symbol in open_positions:
        return False
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return False
    if action != "BUY":
        return False

    # Capital dinámico
    trade_amount = get_trade_amount()
    if trade_amount < 10:
        log.warning(f"Capital dinámico insuficiente: ${trade_amount:.2f}")
        return False

    raw_qty  = trade_amount / price
    quantity = _round_quantity(symbol, raw_qty)
    if not quantity or quantity <= 0:
        return False

    result = place_market_order(symbol, "BUY", quantity)
    if not result:
        return False

    exec_price = float(result.get("fills", [{}])[0].get("price", price)) \
                 if result.get("fills") else price

    price_dec   = get_price_precision(symbol)
    stop_loss   = round(exec_price * (1 - STOP_LOSS_PCT),   price_dec)
    take_profit = round(exec_price * (1 + TAKE_PROFIT_PCT), price_dec)

    open_positions[symbol] = {
        "side": "BUY", "entry_price": exec_price,
        "quantity": quantity, "stop_loss": stop_loss,
        "take_profit": take_profit, "peak_pnl": 0.0,
        "target_reached": False, "prev_price": exec_price,
        "amount_usdt": trade_amount,
    }

    # Guardar en DB
    try:
        from database import save_trade
        save_trade(symbol, "BUY", exec_price, quantity, trade_amount,
                   stop_loss, take_profit, 0.0, "SIGNAL")
    except Exception as e:
        log.warning(f"DB save_trade BUY error: {e}")

    log_trade(symbol, "BUY", exec_price, trade_amount,
              quantity, stop_loss, take_profit, confirmations, indicators)
    alert_trade(symbol, "BUY", exec_price, stop_loss, take_profit, confirmations)
    return True


# Backup al cierre de vela
def check_exit_conditions(symbol: str, current_price: float,
                          high_price: float = None) -> bool:
    if symbol not in open_positions:
        return False
    pos      = open_positions[symbol]
    check_tp = high_price if high_price else current_price
    hit_tp   = check_tp >= pos["take_profit"] and not pos.get("target_reached")
    hit_sl   = current_price <= pos["stop_loss"]
    if not (hit_tp or hit_sl):
        return False
    reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
    return _close_position(symbol, reason, current_price)
