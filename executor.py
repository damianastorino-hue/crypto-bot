# ============================================================
#  SCALPING BOT — executor.py  v5
#  Capital dinámico + trailing stop momentum + force_sell
# ============================================================

import hmac
import hashlib
import time
import requests
from logger import log, log_trade, alert_trade, alert_close, sheets_log_buy, sheets_log_sell
from config import (
    API_KEY, API_SECRET, BASE_URL,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    MAX_OPEN_POSITIONS,
)

# ============================================================
#  Estado global
# ============================================================
open_positions: dict        = {}
_price_prec_cache: dict     = {}   # symbol → int (decimales precio)
_lot_size_cache: dict       = {}   # symbol → {step, min_qty, precision}


# ============================================================
#  Helpers REST
# ============================================================
def _ts() -> int:
    return int(time.time() * 1000)


def _sign(params: dict) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}


def _get(path: str, params: dict = None, timeout: int = 10) -> dict | None:
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=_headers(),
                         params=params, timeout=timeout)
        return r.json()
    except Exception as e:
        log.error(f"GET {path} error: {e}")
        return None


def _post(path: str, params: dict, timeout: int = 10) -> dict | None:
    try:
        r = requests.post(f"{BASE_URL}{path}", headers=_headers(),
                          params=params, timeout=timeout)
        return r.json()
    except Exception as e:
        log.error(f"POST {path} error: {e}")
        return None


# ============================================================
#  Precarga de filtros (una sola request al arrancar)
# ============================================================
def preload_symbol_filters(symbols: list):
    try:
        r = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=15)
        loaded = 0
        for s in r.json().get("symbols", []):
            sym = s["symbol"]
            if sym not in symbols:
                continue
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step  = float(f["stepSize"])
                    minq  = float(f.get("minQty", step))
                    prec  = 0 if step >= 1 else len(str(step).rstrip("0").split(".")[-1])
                    _lot_size_cache[sym] = {"step": step, "min_qty": minq, "precision": prec}
                elif f["filterType"] == "PRICE_FILTER":
                    tick = f["tickSize"].rstrip("0")
                    _price_prec_cache[sym] = len(tick.split(".")[-1]) if "." in tick else 0
            loaded += 1
        log.info(f"Filtros precargados: {loaded}/{len(symbols)} pares")
    except Exception as e:
        log.error(f"preload_symbol_filters: {e}")


def _get_price_precision(symbol: str) -> int:
    return _price_prec_cache.get(symbol, 6)


def _round_quantity(symbol: str, raw_qty: float) -> float | None:
    """Ajusta cantidad al stepSize. Retorna None si queda bajo minQty."""
    info = _lot_size_cache.get(symbol)
    if not info:
        # Fallback si el símbolo no está en caché
        log.warning(f"_round_quantity: {symbol} no en caché, usando redondeo genérico")
        return round(raw_qty, 6)
    step = info["step"]
    qty  = raw_qty - (raw_qty % step)
    qty  = int(qty) if step >= 1 else round(qty, info["precision"])
    return qty if qty >= info["min_qty"] else None


# ============================================================
#  Consultas Binance
# ============================================================
def get_current_price(symbol: str) -> float | None:
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        return float(r.json()["price"])
    except Exception as e:
        log.warning(f"get_current_price {symbol}: {e}")
        return None


def get_balance(asset: str = "USDT") -> float:
    params = {"timestamp": _ts()}
    params["signature"] = _sign(params)
    data = _get("/api/v3/account", params)
    if data:
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    return 0.0


def _get_real_balance(symbol: str) -> float:
    """Balance libre del activo subyacente (sin USDT)."""
    asset = symbol.replace("USDT", "")
    return get_balance(asset)


def get_trade_amount() -> float:
    """
    Capital dinámico: balance_disponible / slots_disponibles.
    Distribuye el capital equitativamente entre posiciones.
    """
    balance   = get_balance("USDT")
    available = MAX_OPEN_POSITIONS - len(open_positions)
    if available <= 0:
        return 0.0
    amount = balance / available
    log.debug(f"Capital dinámico: ${balance:.2f} / {available} slots = ${amount:.2f}")
    return amount


# ============================================================
#  Orden de mercado
# ============================================================
def place_market_order(symbol: str, side: str, quantity: float) -> dict | None:
    params = {
        "symbol":    symbol,
        "side":      side,
        "type":      "MARKET",
        "quantity":  quantity,
        "timestamp": _ts(),
    }
    params["signature"] = _sign(params)
    data = _post("/api/v3/order", params)
    if data and "orderId" in data:
        return data
    log.error(f"Error orden {symbol} {side}: {data}")
    return None


# ============================================================
#  Recuperación de posiciones al reinicio
# ============================================================
def _get_entry_price(symbol: str, qty: float) -> float | None:
    """Precio promedio de entrada usando myTrades (últimas 50 órdenes BUY)."""
    params = {"symbol": symbol, "limit": 50, "timestamp": _ts()}
    params["signature"] = _sign(params)
    data = _get("/api/v3/myTrades", params)
    if not isinstance(data, list):
        return None

    trades = sorted(data, key=lambda x: x["time"], reverse=True)
    total_qty  = 0.0
    total_cost = 0.0
    for t in trades:
        if not t["isBuyer"]:
            continue
        t_qty        = float(t["qty"])
        total_qty   += t_qty
        total_cost  += t_qty * float(t["price"])
        if total_qty >= qty * 0.95:
            return total_cost / total_qty

    return total_cost / total_qty if total_qty > 0 else None


def recover_positions_from_binance() -> int:
    """
    Al arrancar detecta activos comprados en Binance.
    Recupera posición o vende si está en pérdida mayor al SL.
    """
    from config import SYMBOLS

    log.info("Buscando posiciones abiertas en Binance...")
    params = {"timestamp": _ts()}
    params["signature"] = _sign(params)
    data = _get("/api/v3/account", params)
    if not data:
        return 0

    recovered = 0
    for b in data.get("balances", []):
        asset  = b["asset"]
        symbol = f"{asset}USDT"
        if symbol not in SYMBOLS:
            continue

        qty = float(b["free"])
        if qty <= 0:
            if float(b["locked"]) > 0:
                log.warning(f"{symbol}: {b['locked']} locked — ignorando")
            continue

        current_price = get_current_price(symbol)
        if not current_price:
            continue
        if qty * current_price < 5:      # ignorar dust
            continue

        entry_price = _get_entry_price(symbol, qty) or current_price
        pnl_pct     = (current_price - entry_price) / entry_price * 100
        price_dec   = _get_price_precision(symbol)

        if pnl_pct <= -STOP_LOSS_PCT * 100:
            log.warning(f"{symbol}: recuperado en pérdida {pnl_pct:+.2f}% → vendiendo")
            place_market_order(symbol, "SELL", _round_quantity(symbol, qty) or qty)
            continue

        qty_rounded = _round_quantity(symbol, qty)
        if not qty_rounded or qty_rounded <= 0:
            log.warning(f"{symbol}: cantidad inválida tras redondeo — ignorando")
            continue

        open_positions[symbol] = {
            "side":           "BUY",
            "entry_price":    entry_price,
            "quantity":       qty_rounded,
            "stop_loss":      round(entry_price * (1 - STOP_LOSS_PCT),   price_dec),
            "take_profit":    round(entry_price * (1 + TAKE_PROFIT_PCT), price_dec),
            "peak_pnl":       max(0.0, pnl_pct),
            "target_reached": pnl_pct >= TAKE_PROFIT_PCT * 100,
            "prev_price":     current_price,
            "amount_usdt":    qty * current_price,
            "recovered":      True,
        }
        log.info(
            f"[RECUPERADO] {symbol} | entrada:{entry_price:.6f} | "
            f"actual:{current_price:.6f} | P&L:{pnl_pct:+.2f}%"
        )
        recovered += 1

    log.info(f"Posiciones recuperadas: {recovered}")
    return recovered


# ============================================================
#  Cierre de posición (interno)
# ============================================================
def _close_position(symbol: str, reason: str, current_price: float) -> bool:
    if symbol not in open_positions:
        return False
    pos = open_positions[symbol]

    real_balance = _get_real_balance(symbol)
    log.info(f"_close {symbol}: bot_qty={pos['quantity']} real={real_balance}")

    if real_balance <= 0:
        log.error(f"{symbol}: balance real=0, eliminando posición huérfana")
        del open_positions[symbol]
        return False

    qty_to_sell = min(pos["quantity"], real_balance)
    sell_qty    = _round_quantity(symbol, qty_to_sell) or qty_to_sell

    result = place_market_order(symbol, "SELL", sell_qty)
    if not result:
        log.error(f"Error cerrando {symbol} qty={sell_qty}")
        return False

    exec_price = (
        float(result["fills"][0]["price"])
        if result.get("fills") else current_price
    )
    pnl_pct      = (exec_price - pos["entry_price"]) / pos["entry_price"] * 100
    ganancia_usd = round((exec_price - pos["entry_price"]) * pos["quantity"], 4)

    try:
        from database import save_trade
        save_trade(symbol, f"SELL_{reason}", exec_price, pos["quantity"],
                   pos["quantity"] * exec_price, pos["stop_loss"],
                   pos["take_profit"], pnl_pct, reason)
    except Exception as e:
        log.warning(f"DB save_trade error: {e}")

    log_trade(symbol, f"SELL_{reason}", exec_price, 0,
              pos["quantity"], pos["stop_loss"], pos["take_profit"], 0, {})
    alert_close(symbol, reason, pnl_pct, ganancia_usd)
    sheets_log_sell(symbol, pos["entry_price"], exec_price,
                    pos["quantity"], pos.get("amount_usdt", 0),
                    reason, pnl_pct)
    del open_positions[symbol]
    log.info(f"[CERRADO] {symbol} | {reason} | P&L:{pnl_pct:+.2f}% | ${ganancia_usd:+.4f}")
    return True


# ============================================================
#  Vigilancia de posiciones — llamado cada 15s desde bot.py
# ============================================================
def watch_positions() -> list[str]:
    """
    Trailing stop por momentum:
      - SL fijo siempre activo (prioridad)
      - Target (≥ TAKE_PROFIT_PCT) alcanzado + precio baja → VENDE
    """
    closed = []
    for symbol in list(open_positions.keys()):
        pos   = open_positions[symbol]
        price = get_current_price(symbol)
        if price is None:
            continue

        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100

        # Actualizar pico
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

        # Trailing: primer ciclo bajista tras el target
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
#  Backup SL/TP al cierre de vela (cada 1 min)
# ============================================================
def check_exit_conditions(symbol: str, current_price: float,
                          high_price: float = None) -> bool:
    if symbol not in open_positions:
        return False
    pos = open_positions[symbol]

    # Usar high de la vela para detectar TP intracandle
    check_tp = high_price if high_price else current_price
    hit_tp   = check_tp >= pos["take_profit"] and not pos.get("target_reached")
    hit_sl   = current_price <= pos["stop_loss"]

    if not (hit_tp or hit_sl):
        return False
    reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
    return _close_position(symbol, reason, current_price)


# ============================================================
#  Apertura de posición
# ============================================================
def try_open_position(symbol: str, action: str, price: float,
                      confirmations: int, indicators: dict) -> bool:
    if symbol in open_positions:
        return False
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return False
    if action != "BUY":
        return False

    trade_amount = get_trade_amount()
    if trade_amount < 10:
        log.warning(f"Capital insuficiente: ${trade_amount:.2f}")
        return False

    quantity = _round_quantity(symbol, trade_amount / price)
    if not quantity or quantity <= 0:
        return False

    result = place_market_order(symbol, "BUY", quantity)
    if not result:
        return False

    exec_price = (
        float(result["fills"][0]["price"])
        if result.get("fills") else price
    )
    price_dec   = _get_price_precision(symbol)
    stop_loss   = round(exec_price * (1 - STOP_LOSS_PCT),   price_dec)
    take_profit = round(exec_price * (1 + TAKE_PROFIT_PCT), price_dec)

    open_positions[symbol] = {
        "side":           "BUY",
        "entry_price":    exec_price,
        "quantity":       quantity,
        "stop_loss":      stop_loss,
        "take_profit":    take_profit,
        "peak_pnl":       0.0,
        "target_reached": False,
        "prev_price":     exec_price,
        "amount_usdt":    trade_amount,
    }

    try:
        from database import save_trade
        save_trade(symbol, "BUY", exec_price, quantity, trade_amount,
                   stop_loss, take_profit, 0.0, "SIGNAL")
    except Exception as e:
        log.warning(f"DB save_trade BUY: {e}")

    log_trade(symbol, "BUY", exec_price, trade_amount,
              quantity, stop_loss, take_profit, confirmations, indicators)
    alert_trade(symbol, "BUY", exec_price, stop_loss, take_profit, confirmations)
    sheets_log_buy(symbol, exec_price, quantity, trade_amount,
                   stop_loss, take_profit, confirmations)
    return True


# ============================================================
#  Force sell — venta manual desde dashboard
# ============================================================
def force_sell(symbol: str) -> bool:
    if symbol not in open_positions:
        log.warning(f"force_sell: {symbol} no está en posiciones abiertas")
        return False

    # Verificar y corregir cantidad real en Binance
    real_qty = _get_real_balance(symbol)
    if real_qty > 0:
        pos_qty = open_positions[symbol]["quantity"]
        if abs(real_qty - pos_qty) / max(pos_qty, 1e-9) > 0.01:
            log.warning(f"force_sell: ajustando qty {pos_qty} → {real_qty}")
            open_positions[symbol]["quantity"] = _round_quantity(symbol, real_qty) or real_qty

    price = get_current_price(symbol) or open_positions[symbol]["entry_price"]
    log.warning(f"[FORCE SELL] {symbol} @ {price}")
    return _close_position(symbol, "FORCE_SELL", price)


def force_close_all() -> int:
    """Cierra todas las posiciones abiertas. Retorna cuántas cerró."""
    count = 0
    for symbol in list(open_positions.keys()):
        price = get_current_price(symbol) or open_positions[symbol]["entry_price"]
        if _close_position(symbol, "FORCE_SELL_ALL", price):
            count += 1
    return count
