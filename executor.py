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
_lot_size_cache: dict = {}   # { symbol: { step, min_qty, precision } }


def preload_symbol_filters(symbols: list):
    """
    Precarga stepSize y tickSize de todos los pares al arrancar.
    Una sola request a exchangeInfo para todos.
    """
    try:
        r = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", timeout=15)
        data = r.json()
        loaded = 0
        for s in data.get("symbols", []):
            sym = s["symbol"]
            if sym not in symbols:
                continue
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step  = float(f["stepSize"])
                    minq  = float(f.get("minQty", step))
                    prec  = 0 if step >= 1 else len(str(step).rstrip("0").split(".")[-1])
                    _lot_size_cache[sym] = {"step": step, "min_qty": minq, "precision": prec}
                if f["filterType"] == "PRICE_FILTER":
                    tick = f["tickSize"].rstrip("0")
                    prec = len(tick.split(".")[-1]) if "." in tick else 0
                    _price_precision_cache[sym] = prec
            loaded += 1
        log.info(f"Filtros precargados: {loaded}/{len(symbols)} pares")
    except Exception as e:
        log.error(f"preload_symbol_filters error: {e}")


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
    """Redondea cantidad al stepSize. Usa cache si disponible."""
    # Usar cache precargado (más rápido y confiable)
    if symbol in _lot_size_cache:
        c     = _lot_size_cache[symbol]
        step  = c["step"]
        qty   = raw_qty - (raw_qty % step)
        qty   = int(qty) if step >= 1 else round(qty, c["precision"])
        return qty if qty >= c["min_qty"] else None

    # Fallback: consultar API
    info = get_symbol_info(symbol)
    if not info:
        return round(raw_qty, 0) if raw_qty >= 1 else round(raw_qty, 6)
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step    = float(f["stepSize"])
            min_qty = float(f.get("minQty", step))
            qty     = raw_qty - (raw_qty % step)
            qty     = int(qty) if step >= 1 else round(qty, len(str(step).rstrip("0").split(".")[-1]))
            # Guardar en cache para próximas veces
            _lot_size_cache[symbol] = {"step": step, "min_qty": min_qty,
                                        "precision": 0 if step >= 1 else len(str(step).rstrip("0").split(".")[-1])}
            return qty if qty >= min_qty else None
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



# ============================================================
#  RECUPERACIÓN DE POSICIONES AL REINICIO
# ============================================================
def recover_positions_from_binance() -> int:
    """
    Al arrancar, consulta Binance para detectar activos comprados.
    Usa myTrades para obtener el precio de entrada real.
    Evalúa si mantener o vender inmediatamente.
    Retorna cantidad de posiciones recuperadas.
    """
    from config import SYMBOLS

    log.info("Buscando posiciones abiertas en Binance...")

    # 1. Traer balances
    params = {"timestamp": _timestamp()}
    params["signature"] = _sign(params)
    try:
        r = requests.get(f"{BASE_URL}/api/v3/account",
                         headers=_headers(), params=params, timeout=10)
        balances = r.json().get("balances", [])
    except Exception as e:
        log.error(f"recover: error obteniendo cuenta: {e}")
        return 0

    recovered = 0

    for b in balances:
        asset = b["asset"]
        symbol = f"{asset}USDT"

        # Solo pares que el bot monitorea
        if symbol not in SYMBOLS:
            continue

        # Solo usar balance FREE — el locked no se puede vender
        qty_free   = float(b["free"])
        qty_locked = float(b["locked"])
        qty = qty_free

        if qty <= 0:
            if qty_locked > 0:
                log.warning(f"{symbol}: tiene {qty_locked} locked (Earn/orden pendiente) — ignorando")
            continue

        # Precio actual
        current_price = get_current_price(symbol)
        if not current_price:
            continue

        value_usdt = qty * current_price
        if value_usdt < 5:  # ignorar dust
            continue

        # 2. Buscar precio de entrada en myTrades
        entry_price = _get_entry_price(symbol, qty)
        if not entry_price:
            entry_price = current_price  # fallback: usar precio actual
            log.warning(f"{symbol}: no encontré precio de entrada, usando precio actual")

        # 3. Calcular P&L actual
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        price_dec = get_price_precision(symbol)
        stop_loss   = round(entry_price * (1 - STOP_LOSS_PCT),   price_dec)
        take_profit = round(entry_price * (1 + TAKE_PROFIT_PCT), price_dec)

        # 4. Evaluar si mantener o vender
        if pnl_pct <= -STOP_LOSS_PCT * 100:
            log.warning(f"{symbol}: recuperado en pérdida ({pnl_pct:+.2f}%) → vendiendo")
            place_market_order(symbol, "SELL", qty)
            continue

        # Redondear cantidad al stepSize del par antes de guardar
        qty_rounded = _round_quantity(symbol, qty)
        if not qty_rounded or qty_rounded <= 0:
            log.warning(f"{symbol}: cantidad inválida tras redondeo ({qty}), ignorando")
            continue

        # Mantener la posición
        open_positions[symbol] = {
            "side":           "BUY",
            "entry_price":    entry_price,
            "quantity":       qty_rounded,
            "stop_loss":      stop_loss,
            "take_profit":    take_profit,
            "peak_pnl":       max(0.0, pnl_pct),
            "target_reached": pnl_pct >= TAKE_PROFIT_PCT * 100,
            "prev_price":     current_price,
            "amount_usdt":    value_usdt,
            "recovered":      True,
        }

        log.info(
            f"[RECUPERADO] {symbol} | entrada:{entry_price:.6f} | "
            f"actual:{current_price:.6f} | P&L:{pnl_pct:+.2f}% | "
            f"SL:{stop_loss:.6f} TP:{take_profit:.6f}"
        )
        recovered += 1

    log.info(f"Posiciones recuperadas: {recovered}")
    return recovered


def _get_entry_price(symbol: str, qty: float) -> float | None:
    """
    Busca el precio promedio de entrada usando myTrades.
    Toma las últimas órdenes BUY hasta cubrir la cantidad actual.
    """
    params = {
        "symbol":    symbol,
        "limit":     50,
        "timestamp": _timestamp(),
    }
    params["signature"] = _sign(params)
    try:
        r = requests.get(f"{BASE_URL}/api/v3/myTrades",
                         headers=_headers(), params=params, timeout=10)
        trades = r.json()
        if not isinstance(trades, list):
            return None

        # Ordenar de más reciente a más antiguo
        trades.sort(key=lambda x: x["time"], reverse=True)

        total_qty   = 0.0
        total_cost  = 0.0

        for t in trades:
            if not t["isBuyer"]:
                continue
            t_qty   = float(t["qty"])
            t_price = float(t["price"])
            total_qty  += t_qty
            total_cost += t_qty * t_price
            if total_qty >= qty * 0.95:  # 95% de tolerancia
                return total_cost / total_qty

        return total_cost / total_qty if total_qty > 0 else None

    except Exception as e:
        log.error(f"myTrades error {symbol}: {e}")
        return None

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


def _safe_sell_quantity(symbol: str, qty: float) -> float:
    """
    Obtiene la cantidad válida para vender.
    Intenta obtener stepSize de Binance, si falla usa fallback inteligente.
    """
    try:
        rounded = _round_quantity(symbol, qty)
        if rounded and rounded > 0:
            return rounded
    except Exception:
        pass
    # Fallback: si el precio es < $1 (tokens baratos como MANA, TRX, DOGE)
    # probablemente tiene stepSize=1 → usar entero
    price = get_current_price(symbol) or 1.0
    if price < 1.0:
        return float(int(qty))  # entero puro
    elif price < 10.0:
        return round(qty, 1)
    elif price < 100.0:
        return round(qty, 2)
    else:
        return round(qty, 4)


def _get_real_balance(symbol: str) -> float:
    """Balance libre real del activo en Binance."""
    asset = symbol.replace("USDT", "")
    params = {"timestamp": _timestamp()}
    params["signature"] = _sign(params)
    try:
        r = requests.get(f"{BASE_URL}/api/v3/account",
                         headers=_headers(), params=params, timeout=10)
        for b in r.json().get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
    except Exception as e:
        log.warning(f"_get_real_balance {symbol}: {e}")
    return 0.0


def _close_position(symbol: str, reason: str, current_price: float) -> bool:
    """Ejecuta SELL consultando balance real antes de enviar la orden."""
    if symbol not in open_positions:
        return False
    pos = open_positions[symbol]

    # Consultar balance REAL en Binance
    real_balance = _get_real_balance(symbol)
    log.info(f"_close_position {symbol}: bot_qty={pos['quantity']} real_balance={real_balance}")

    if real_balance <= 0:
        log.error(f"{symbol}: balance real=0, eliminando posición huérfana")
        del open_positions[symbol]
        return False

    # Usar el menor: lo que el bot cree vs lo que realmente hay
    qty_to_sell = min(pos["quantity"], real_balance)
    sell_qty    = _safe_sell_quantity(symbol, qty_to_sell)
    log.info(f"{symbol}: vendiendo {sell_qty} (de {qty_to_sell} disponibles)")

    result = place_market_order(symbol, "SELL", sell_qty)
    if not result:
        log.error(f"Error cerrando {symbol} sell_qty={sell_qty}")
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

    pos   = open_positions[symbol]
    price = get_current_price(symbol) or pos["entry_price"]

    # Verificar cantidad real disponible en Binance (puede diferir del registro)
    params = {"timestamp": _timestamp()}
    params["signature"] = _sign(params)
    try:
        r    = requests.get(f"{BASE_URL}/api/v3/account",
                            headers=_headers(), params=params, timeout=10)
        bals = r.json().get("balances", [])
        asset = symbol.replace("USDT", "")
        for b in bals:
            if b["asset"] == asset:
                real_qty = float(b["free"])
                if real_qty > 0 and abs(real_qty - pos["quantity"]) / pos["quantity"] > 0.01:
                    log.warning(f"force_sell: ajustando qty {pos['quantity']} → {real_qty}")
                    open_positions[symbol]["quantity"] = _round_quantity(symbol, real_qty) or real_qty
                break
    except Exception as e:
        log.warning(f"force_sell: no pude verificar balance real: {e}")

    log.warning(f"[FORCE SELL] {symbol} @ {price} qty:{open_positions[symbol]['quantity']}")
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
