# ============================================================
#  MÓDULO: bot.py  —  NÚCLEO PRINCIPAL
#  WebSocket Binance → Buffer OHLCV → Indicadores → Señales → Órdenes
# ============================================================

import asyncio
import json
import signal
from collections import deque
from datetime import datetime, timezone

import websockets

from config import SYMBOLS, KLINE_INTERVAL, KLINE_BUFFER
from indicators import compute_indicators, generate_signal
from executor import try_open_position, check_exit_conditions, force_close_all, open_positions, get_balance
from logger import log
import dashboard as dash


# ============================================================
#  Buffer de velas por símbolo
# ============================================================
candle_buffer: dict[str, deque] = {
    sym: deque(maxlen=KLINE_BUFFER) for sym in SYMBOLS
}

current_candle: dict[str, dict | None] = {sym: None for sym in SYMBOLS}

# Contador global de mensajes recibidos (diagnóstico)
_msg_count = 0


# ============================================================
#  Procesamiento de mensaje WebSocket
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
        log.debug(f"{symbol} vela cerrada | close={candle['close']:.4f} | buf={len(candle_buffer[symbol])}")
        on_candle_close(symbol, candle["close"])


def on_candle_close(symbol: str, price: float):
    check_exit_conditions(symbol, price)

    buf = list(candle_buffer[symbol])
    ind = compute_indicators(buf)
    if ind is None:
        return

    action, confirmations, detail = generate_signal(ind)

    # Actualizar semáforo siempre (cada vela)
    estado = detail.get("estado", "NEUTRO")
    dash.push_semaphore(symbol, action, ind["rsi"], estado, confirmations)

    if action != "HOLD":
        log.info(
            f"[SEÑAL] {symbol} → {action} | conf:+{confirmations} | "
            f"RSI:{detail['rsi']} MACD:{detail['macd']} EMA:{detail['ema']}"
        )
        dash.push_signal(symbol, action, confirmations, detail, price)

    if action == "BUY":
        try_open_position(symbol, action, price, confirmations, detail)


# ============================================================
#  WebSocket — stream combinado (Binance REAL)
# ============================================================
def build_ws_url() -> str:
    streams = "/".join(
        f"{sym.lower()}@kline_{KLINE_INTERVAL}"
        for sym in SYMBOLS
    )
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


async def listen():
    url = build_ws_url()
    log.info(f"WebSocket URL: {url[:60]}...")
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
#  Monitor periódico
# ============================================================
async def status_monitor():
    while True:
        await asyncio.sleep(30)
        n_pos    = len(open_positions)
        # Contar pares con AL MENOS 1 vela (no 30)
        pares_vivos   = sum(1 for b in candle_buffer.values() if len(b) >= 1)
        pares_listos  = sum(1 for b in candle_buffer.values() if len(b) >= 30)
        now = datetime.now(timezone.utc).strftime('%H:%M:%S')

        log.info(
            f"[STATUS] {now} UTC | Pos: {n_pos} | "
            f"Pares recibiendo: {pares_vivos}/20 | "
            f"Pares listos (≥30 velas): {pares_listos}/20 | "
            f"Msgs recibidos: {_msg_count}"
        )

        for sym, pos in open_positions.items():
            log.info(
                f"  → {sym} entry:{pos['entry_price']:.4f} "
                f"SL:{pos['stop_loss']:.4f} TP:{pos['take_profit']:.4f}"
            )

        # Sincronizar estado al dashboard
        dash.update_state("open_positions", dict(open_positions))
        dash.update_state("candle_buffer",  dict(candle_buffer))
        try:
            dash.update_state("balance_usdt", get_balance("USDT"))
        except Exception:
            pass


# ============================================================
#  Shutdown limpio
# ============================================================
def handle_shutdown(loop):
    log.warning("Señal de cierre recibida. Cerrando posiciones...")
    force_close_all()
    log.info("Bot detenido.")
    loop.stop()


# ============================================================
#  MAIN
# ============================================================
async def main():
    log.info("=" * 55)
    log.info("  SCALPING BOT — Binance REAL Spot")
    log.info(f"  Pares: {len(SYMBOLS)} | Intervalo: {KLINE_INTERVAL}")
    log.info(f"  SL: -1.5% | TP: +2.5% | Max pos: 3")
    log.info("=" * 55)

    dash.update_state("candle_buffer", dict(candle_buffer))

    from config import DASHBOARD_PORT
    dash.start_dashboard(host="0.0.0.0", port=DASHBOARD_PORT)
    log.info(f"  Dashboard: http://localhost:{DASHBOARD_PORT}")
    log.info("=" * 55)

    await asyncio.gather(
        listen(),
        status_monitor(),
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
