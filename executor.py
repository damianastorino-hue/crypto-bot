# ============================================================
#  MÓDULO: executor.py  v3
#  Trailing stop por momentum: vende en primer ciclo bajista
#  después de alcanzar el target
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
#  Estado global de posiciones
#  { symbol: {
#      entry_price, quantity, stop_loss, take_profit,
#      peak_pnl,        ← máximo P&L alcanzado
#      target_reached,  ← True si superó TAKE_PROFIT_PCT
#      prev_price,      ← precio del ciclo anterior (15s)
#  }}
# ============================================================
open_positions: dict = {}

# Cache de precisión de precio por par
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
    """Precio actual via REST (para el loop de vigilancia cada 15s)."""
    try:
        r = requests.get(
            f"{BASE_URL}/api/v3/ticker/price",
            params={"symbol": symbol}, timeout=5
        )
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"get_current_price {symbol}: {e}")
        return None


def get_balance(asset: str = "USDT") -> float:
    params = {"timestamp": _timestamp()}
    params["signature"] = _sign(params)
    try:
        r = requests.get(
            f"{BASE_URL}/api/v3/account",
            headers=_headers(), params=params, timeout=10
        )
        for b in r.json().get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    except Exception as e:
        log.error(f"get_balance error: {e}")
    return 0.0


def place_market_order(symbol: str, side: str, quantity: float) -> dict | None:
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
            return data
        else:
            log.error(f"Error orden {symbol} {side}: {data}")
            return None
    except Exception as e:
        log.error(f"place_market_order exception: {e}")
        return None


def _close_position(symbol: str, reason: str, current_price: float):
    """Ejecuta el SELL y limpia la posición."""
    pos = open_positions[symbol]
    result = place_market_order(symbol, "SELL", pos["quantity"])
    if not result:
        log.error(f"Error cerrando {symbol}")
        return False

    exec_price = float(result.get("fills", [{}])[0].get("price", current_price)) \
                 if result.get("fills") else current_price
    pnl_pct = ((exec_price - pos["entry_price"]) / pos["entry_price"]) * 100

    log_trade(symbol, f"SELL_{reason}", exec_price, 0,
              pos["quantity"], pos["stop_loss"], pos["take_profit"], 0, {})
    alert_close(symbol, reason, pnl_pct)
    del open_positions[symbol]
    log.info(f"[CERRADO] {symbol} | {reason} | P&L: {pnl_pct:+.2f}%")
    return True


# ============================================================
#  VIGILANCIA EN TIEMPO REAL (llamado cada 15s desde bot.py)
# ============================================================
def watch_positions() -> list[str]:
    """
    Verifica todas las posiciones abiertas con precio actual.
    Lógica trailing stop por momentum:

      1. Stop Loss fijo → vender siempre
      2. Si P&L >= TARGET → marcar target_reached = True
      3. Con target alcanzado:
           - precio subió o igual → actualizar peak, seguir
           - precio BAJÓ          → VENDER (primer ciclo bajista)

    Retorna lista de símbolos cerrados.
    """
    closed = []
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_current_price(symbol)
        if price is None:
            continue

        pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100

        # Actualizar peak P&L
        if pnl_pct > pos.get("peak_pnl", 0):
            open_positions[symbol]["peak_pnl"] = pnl_pct

        # --- Stop Loss fijo: siempre tiene prioridad ---
        if price <= pos["stop_loss"]:
            _close_position(symbol, "STOP_LOSS", price)
            closed.append(symbol)
            continue

        # --- Target alcanzado? ---
        if pnl_pct >= TAKE_PROFIT_PCT * 100:
            open_positions[symbol]["target_reached"] = True

        # --- Trailing stop por momentum ---
        if pos.get("target_reached"):
            prev = pos.get("prev_price")
            if prev is not None and price < prev:
                # Primer ciclo bajista después del target → VENDER
                log.info(f"[TRAILING] {symbol} | precio bajó {prev:.6f}→{price:.6f} | P&L: {pnl_pct:+.2f}%")
                _close_position(symbol, "TRAILING_STOP", price)
                closed.append(symbol)
                continue

        # Guardar precio actual para comparar en el próximo ciclo
        open_positions[symbol]["prev_price"] = price

    return closed


# ============================================================
#  ENTRADA
# ============================================================
def try_open_position(symbol: str, action: str, price: float,
                      confirmations: int, indicators: dict) -> bool:
    if symbol in open_positions:
        return False
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return False

    usdt_balance = get_balance("USDT")
    if usdt_balance < TRADE_AMOUNT_USDT:
        log.warning(f"Balance insuficiente: {usdt_balance:.2f} USDT")
        return False

    raw_qty  = TRADE_AMOUNT_USDT / price
    quantity = _round_quantity(symbol, raw_qty)
    if not quantity or quantity <= 0:
        return False

    if action != "BUY":
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
        "side":            "BUY",
        "entry_price":     exec_price,
        "quantity":        quantity,
        "stop_loss":       stop_loss,
        "take_profit":     take_profit,
        "peak_pnl":        0.0,
        "target_reached":  False,
        "prev_price":      exec_price,
    }

    log_trade(symbol, "BUY", exec_price, TRADE_AMOUNT_USDT,
              quantity, stop_loss, take_profit, confirmations, indicators)
    alert_trade(symbol, "BUY", exec_price, stop_loss, take_profit, confirmations)
    return True


# ============================================================
#  SALIDA POR VELA (SL/TP usando high de la vela — backup)
# ============================================================
def check_exit_conditions(symbol: str, current_price: float,
                          high_price: float = None) -> bool:
    """
    Verificación al cierre de vela — backup del watch_positions.
    Solo actúa si watch_positions no cerró antes.
    """
    if symbol not in open_positions:
        return False
    pos = open_positions[symbol]
    check_tp = high_price if high_price else current_price
    hit_tp   = check_tp       >= pos["take_profit"] and not pos.get("target_reached")
    hit_sl   = current_price  <= pos["stop_loss"]
    if not (hit_tp or hit_sl):
        return False
    reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
    return _close_position(symbol, reason, current_price)


def force_close_all():
    for symbol in list(open_positions.keys()):
        pos = open_positions[symbol]
        log.warning(f"Force close {symbol}")
        place_market_order(symbol, "SELL", pos["quantity"])
        del open_positions[symbol]
