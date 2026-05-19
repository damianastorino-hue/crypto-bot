# ============================================================
#  MÓDULO: bot.py  v4
#  Reinicio inteligente + SQLite + vigilancia 15s
# ============================================================

import asyncio
import json
import signal
from collections import deque
from datetime import datetime, timezone

import websockets

from config import SYMBOLS, KLINE_INTERVAL, KLINE_BUFFER
from indicators import compute_indicators, generate_signal
from executor import (try_open_position, check_exit_conditions,
                      force_close_all, open_positions, get_balance,
                      watch_positions, recover_positions_from_binance,
                      preload_symbol_filters)
from logger import log
import dashboard as dash
import database as db


# ============================================================
#  Buffer de velas
# ============================================================
candle_buffer: dict[str, deque] = {
    sym: deque(maxlen=KLINE_BUFFER) for sym in SYMBOLS
}
current_candle: dict[str, dict | None] = {sym: None for sym in SYMBOLS}
_msg_count = 0


# ============================================================
#  REINICIO INTELIGENTE
# ============================================================
def load_candles_from_db():
    """
    Si el bot estuvo offline < 5 minutos, carga las velas
    guardadas en SQLite para no esperar 30 minutos.
    """
    if not db.was_recently_stopped(max_minutes=5):
        log.info("Offline > 5 min — acumulando velas desde WebSocket (30 min)")
        return False

    log.info("Offline < 5 min — cargando velas desde SQLite...")
    loaded = 0
    for sym in SYMBOLS:
        candles = db.load_recent_candles(sym, limit=KLINE_BUFFER)
        if len(candles) >= 30:
            candle_buffer[sym].extend(candles)
            loaded += 1
    log.info(f"Pares recuperados con ≥30 velas: {loaded}/{len(SYMBOLS)}")
    return loaded > 0


# ============================================================
#  Procesamiento WebSocket
# ============================================================
def process_kline(symbol: str, kline: dict):
    global _msg_count
    _msg_count += 1

    is_closed = kline["x"]
    candle = {
        "open":   float(kline["o"]),
        "high":   float(kline["h"]),
        "low":    float(kline["l"]),
        "close":  float(kline["c"]),
        "volume": float(kline["v"]),
    }
    current_candle[symbol] = candle

    if is_closed:
        candle_buffer[symbol].append(candle)
        # Guardar vela en SQLite
        try:
            db.save_candle(symbol, candle)
        except Exception:
            pass
        on_candle_close(symbol, candle["close"], candle["high"])


def on_candle_close(symbol: str, price: float, high: float = None):
    check_exit_conditions(symbol, price, high_price=high)

    buf = list(candle_buffer[symbol])
    ind = compute_indicators(buf)
    if ind is None:
        return

    action, confirmations, detail = generate_signal(ind)

    # Guardar señal en SQLite
    try:
        db.save_signal(symbol, ind, action, confirmations)
    except Exception:
        pass

    estado = detail.get("estado", "NEUTRO")
    dash.push_semaphore(symbol, action, ind["rsi"], estado, confirmations)

    if action != "HOLD":
        log.info(
            f"[SEÑAL] {symbol} → {action} | conf:+{confirmations} | "
            f"RSI:{detail['rsi']} MACD:{detail['macd']} EMA:{detail['ema']}"
        )
        dash.push_signal(symbol, action, confirmations, detail, price)

    if action == "BUY":
        if not dash.is_bot_active():
            log.debug(f"Bot DETENIDO — BUY {symbol} ignorado")
        elif dash.is_buying_paused():
            log.debug(f"Compras PAUSADAS — BUY {symbol} ignorado")
        else:
            try_open_position(symbol, action, price, confirmations, detail)


# ============================================================
#  WebSocket
# ============================================================
def build_ws_url() -> str:
    streams = "/".join(f"{sym.lower()}@kline_{KLINE_INTERVAL}" for sym in SYMBOLS)
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


async def listen():
    url = build_ws_url()
    log.info(f"WebSocket: {len(SYMBOLS)} pares | {KLINE_INTERVAL}")
    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                log.info("✅ WebSocket conectado a Binance REAL")
                reconnect_delay = 5
                async for raw_msg in ws:
                    msg = json.loads(raw_msg)
                    if "data" not in msg:
                        continue
                    data   = msg["data"]
                    symbol = data["s"]
                    kline  = data["k"]
                    if symbol in candle_buffer:
                        process_kline(symbol, kline)
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"WebSocket cerrado: {e}. Reconectando en {reconnect_delay}s...")
        except Exception as e:
            log.error(f"Error WebSocket: {e}. Reconectando en {reconnect_delay}s...")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


# ============================================================
#  VIGILANCIA POSICIONES — cada 15s
# ============================================================
async def position_watcher():
    await asyncio.sleep(15)
    while True:
        # Trailing/SL solo si bot está activo (no en modo stop)
        if open_positions and dash.is_bot_active():
            try:
                closed = await asyncio.get_event_loop().run_in_executor(
                    None, watch_positions)
                if closed:
                    dash.update_state("open_positions", dict(open_positions))
            except Exception as e:
                log.error(f"position_watcher error: {e}")
        await asyncio.sleep(15)


# ============================================================
#  STATUS MONITOR — cada 30s
# ============================================================
async def status_monitor():
    while True:
        await asyncio.sleep(30)
        n_pos        = len(open_positions)
        pares_vivos  = sum(1 for b in candle_buffer.values() if len(b) >= 1)
        pares_listos = sum(1 for b in candle_buffer.values() if len(b) >= 30)
        now          = datetime.now(timezone.utc).strftime('%H:%M:%S')

        log.info(
            f"[STATUS] {now} | Pos:{n_pos} | "
            f"Recibiendo:{pares_vivos}/40 | "
            f"Listos:{pares_listos}/40 | Msgs:{_msg_count}"
        )

        # Heartbeat para reinicio inteligente
        try:
            db.update_heartbeat()
        except Exception:
            pass

        # Sincronizar dashboard
        dash.update_state("open_positions", dict(open_positions))
        dash.update_state("candle_buffer",  dict(candle_buffer))
        try:
            dash.update_state("balance_usdt", get_balance("USDT"))
        except Exception:
            pass

        # Limpiar velas viejas cada 30 min (aprox)
        if _msg_count % 36000 == 0:
            try:
                db.clean_old_candles(days=3)
            except Exception:
                pass


# ============================================================
#  SHUTDOWN
# ============================================================
def handle_shutdown(loop):
    log.warning("Cerrando bot...")
    try:
        db.update_heartbeat()
    except Exception:
        pass
    force_close_all()
    log.info("Bot detenido.")
    loop.stop()


# ============================================================
#  MAIN
# ============================================================
async def main():
    log.info("=" * 55)
    log.info("  SCALPING BOT v4 — Binance REAL Spot")
    log.info(f"  Pares: {len(SYMBOLS)} | Intervalo: {KLINE_INTERVAL}")
    log.info(f"  SL:-1.5% | TP:+0.8% trailing | MaxPos:{3} | Watch:15s")
    log.info("=" * 55)

    # Inicializar SQLite
    db.init_db()

    # Precargar filtros de todos los pares (stepSize, tickSize)
    # Una sola request para todos — evita errores LOT_SIZE en órdenes
    preload_symbol_filters(SYMBOLS)

    # Recuperar posiciones abiertas de Binance
    try:
        n_recovered = recover_positions_from_binance()
        if n_recovered:
            dash.update_state("open_positions", dict(open_positions))
            log.info(f"✅ {n_recovered} posiciones recuperadas de Binance")
    except Exception as e:
        log.warning(f"No se pudo recuperar posiciones: {e}")

    # Reinicio inteligente
    recovered = load_candles_from_db()
    if recovered:
        log.info("✅ Velas recuperadas — operativo en ~2 min")
    else:
        log.info("⏳ Acumulando velas — operativo en ~30 min")

    dash.update_state("candle_buffer", dict(candle_buffer))

    from config import DASHBOARD_PORT
    dash.start_dashboard(host="0.0.0.0", port=DASHBOARD_PORT)
    log.info(f"  Dashboard: http://localhost:{DASHBOARD_PORT}")
    log.info("=" * 55)

    await asyncio.gather(
        listen(),
        status_monitor(),
        position_watcher(),
    )


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_shutdown, loop)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        handle_shutdown(loop)
    finally:
        loop.close()
