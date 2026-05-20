# ============================================================
#  SCALPING BOT — bot.py  v5
#  WebSocket + reinicio inteligente + vigilancia 15s
# ============================================================

import asyncio
import json
import os
import signal
from collections import deque
from datetime import datetime, timezone

import websockets

from config import SYMBOLS, KLINE_INTERVAL, KLINE_BUFFER, MAX_OPEN_POSITIONS, DASHBOARD_PORT
from indicators import compute_indicators, generate_signal
from executor import (
    try_open_position, check_exit_conditions,
    open_positions, get_balance, watch_positions,
    recover_positions_from_binance, preload_symbol_filters,
)
from logger import log
import dashboard as dash
import database as db


# ============================================================
#  Buffers de velas
# ============================================================
candle_buffer: dict[str, deque] = {sym: deque(maxlen=KLINE_BUFFER) for sym in SYMBOLS}
current_candle: dict[str, dict | None] = {sym: None for sym in SYMBOLS}
_msg_count = 0


# ============================================================
#  Reinicio inteligente
# ============================================================
def load_candles_from_db() -> bool:
    if not db.was_recently_stopped(max_minutes=5):
        log.info("Offline > 5 min — acumulando velas desde WebSocket (~30 min)")
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

    candle = {
        "open":   float(kline["o"]),
        "high":   float(kline["h"]),
        "low":    float(kline["l"]),
        "close":  float(kline["c"]),
        "volume": float(kline["v"]),
    }
    current_candle[symbol] = candle

    if kline["x"]:   # vela cerrada
        candle_buffer[symbol].append(candle)
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

    try:
        db.save_signal(symbol, ind, action, confirmations)
    except Exception:
        pass

    estado = detail.get("estado", "NEUTRO")
    dash.push_semaphore(symbol, action, ind["rsi"], estado, confirmations)

    if action != "HOLD":
        log.info(
            f"[SEÑAL] {symbol} → {action} | conf:{confirmations} | "
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
def _build_ws_url() -> str:
    streams = "/".join(f"{s.lower()}@kline_{KLINE_INTERVAL}" for s in SYMBOLS)
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


async def listen():
    url = _build_ws_url()
    log.info(f"WebSocket: {len(SYMBOLS)} pares | {KLINE_INTERVAL}")
    delay = 5

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                log.info("✅ WebSocket conectado — Binance REAL")
                delay = 5
                async for raw in ws:
                    msg = json.loads(raw)
                    if "data" not in msg:
                        continue
                    data   = msg["data"]
                    symbol = data["s"]
                    if symbol in candle_buffer:
                        process_kline(symbol, data["k"])
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"WebSocket cerrado: {e}. Reintentando en {delay}s...")
        except Exception as e:
            log.error(f"WebSocket error: {e}. Reintentando en {delay}s...")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


# ============================================================
#  Vigilancia posiciones — cada 15s
# ============================================================
async def position_watcher():
    await asyncio.sleep(15)
    while True:
        if open_positions and dash.is_bot_active():
            try:
                closed = await asyncio.get_event_loop().run_in_executor(
                    None, watch_positions)
                if closed:
                    dash.update_state("open_positions", dict(open_positions))
            except Exception as e:
                log.error(f"position_watcher: {e}")
        await asyncio.sleep(15)


# ============================================================
#  Monitor de estado — cada 30s
# ============================================================
async def status_monitor():
    while True:
        await asyncio.sleep(30)
        global _msg_count
        n_pos    = len(open_positions)
        vivos    = sum(1 for b in candle_buffer.values() if len(b) >= 1)
        listos   = sum(1 for b in candle_buffer.values() if len(b) >= 30)
        now      = datetime.now(timezone.utc).strftime("%H:%M:%S")

        log.info(
            f"[STATUS] {now} | Pos:{n_pos} | "
            f"Recibiendo:{vivos}/{len(SYMBOLS)} | "
            f"Listos:{listos}/{len(SYMBOLS)} | Msgs:{_msg_count}"
        )

        try:
            db.update_heartbeat()
        except Exception:
            pass

        dash.update_state("open_positions", dict(open_positions))
        dash.update_state("candle_buffer",  dict(candle_buffer))
        try:
            dash.update_state("balance_usdt", get_balance("USDT"))
        except Exception:
            pass

        # Limpiar velas viejas cada ~30 min
        if _msg_count % 36_000 == 0:
            try:
                db.clean_old_candles(days=3)
            except Exception:
                pass


# ============================================================
#  Shutdown limpio
# ============================================================
def handle_shutdown(loop):
    log.warning("Shutdown — posiciones se mantienen en Binance")
    try:
        db.update_heartbeat()
    except Exception:
        pass
    log.info(f"Posiciones al cierre: {list(open_positions.keys())}")
    loop.stop()


# ============================================================
#  Main
# ============================================================
async def main():
    log.info("=" * 55)
    log.info("  SCALPING BOT v5 — Binance REAL Spot")
    log.info(f"  Pares:{len(SYMBOLS)} | Intervalo:{KLINE_INTERVAL} | MaxPos:{MAX_OPEN_POSITIONS}")
    log.info(f"  SL:-{int(0.015*100)}%  TP:+{int(KLINE_INTERVAL[0])}% trailing  Watch:15s")
    log.info("=" * 55)

    # Volumen persistente Railway
    if os.path.exists("/data"):
        os.environ["DB_PATH"] = "/data/scalping.db"
        log.info("Usando volumen persistente /data/scalping.db")
    else:
        log.warning("Sin volumen persistente — DB local (se pierde al reiniciar)")

    db.init_db()
    preload_symbol_filters(SYMBOLS)

    try:
        n = recover_positions_from_binance()
        if n:
            dash.update_state("open_positions", dict(open_positions))
            log.info(f"✅ {n} posiciones recuperadas")
    except Exception as e:
        log.warning(f"recover_positions: {e}")

    if load_candles_from_db():
        log.info("✅ Velas recuperadas — operativo en ~2 min")
    else:
        log.info("⏳ Acumulando velas — operativo en ~30 min")

    dash.update_state("candle_buffer", dict(candle_buffer))
    dash.start_dashboard(host="0.0.0.0", port=DASHBOARD_PORT)
    log.info(f"Dashboard: http://0.0.0.0:{DASHBOARD_PORT}")
    log.info("=" * 55)

    await asyncio.gather(listen(), status_monitor(), position_watcher())


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
