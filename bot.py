# ============================================================
#  MÓDULO: bot.py  —  NÚCLEO PRINCIPAL
#  WebSocket Binance → Buffer OHLCV → Indicadores → Señales → Órdenes
# ============================================================

import asyncio
import json
import signal
import sys
from collections import deque
from datetime import datetime

import websockets

from config import SYMBOLS, KLINE_INTERVAL, KLINE_BUFFER
from indicators import compute_indicators, generate_signal
from executor import try_open_position, check_exit_conditions, force_close_all, open_positions, get_balance
from logger import log
import dashboard as dash


# ============================================================
#  Buffer de velas por símbolo
#  { "BTCUSDT": deque([{open, high, low, close, volume}, ...]) }
# ============================================================
candle_buffer: dict[str, deque] = {
    sym: deque(maxlen=KLINE_BUFFER) for sym in SYMBOLS
}

# Vela en construcción (se agrega al buffer solo al cerrarse)
current_candle: dict[str, dict | None] = {sym: None for sym in SYMBOLS}


# ============================================================
#  Procesamiento de mensaje WebSocket
# ============================================================
def process_kline(symbol: str, kline: dict):
    """
    Actualiza buffer con datos de la vela.
    Solo agrega al buffer cuando la vela está cerrada (is_closed=True).
    """
    is_closed = kline["x"]
    candle = {
        "open":   float(kline["o"]),
        "high":   float(kline["h"]),
        "low":    float(kline["l"]),
        "close":  float(kline["c"]),
        "volume": float(kline["v"]),
    }

    current_candle[symbol] = candle  # siempre actualiza la vela viva

    if is_closed:
        candle_buffer[symbol].append(candle)
        log.debug(f"{symbol} vela cerrada | close={candle['close']:.4f} | buf={len(candle_buffer[symbol])}")
        on_candle_close(symbol, candle["close"])


def on_candle_close(symbol: str, price: float):
    """
    Ejecutado al cerrar cada vela.
    1) Verifica SL/TP de posición abierta
    2) Calcula indicadores
    3) Genera señal
    4) Abre posición si aplica
    """
    # --- Verificar salida primero ---
    check_exit_conditions(symbol, price)

    # --- Calcular indicadores ---
    buf = list(candle_buffer[symbol])
    ind = compute_indicators(buf)
    if ind is None:
        return  # no hay suficientes datos todavía

    # --- Señal ---
    action, confirmations, detail = generate_signal(ind)

    if action != "HOLD":
        log.info(
            f"[SEÑAL] {symbol} → {action} | conf:{confirmations}/4 | "
            f"RSI:{detail['rsi']} MACD:{detail['macd']} EMA:{detail['ema']}"
        )
        dash.push_signal(symbol, action, confirmations, detail, price)

    # --- Abrir posición ---
    if action == "BUY":
        try_open_position(symbol, action, price, confirmations, detail)


# ============================================================
#  WebSocket — stream combinado de múltiples pares
# ============================================================
def build_ws_url() -> str:
    """
    Combina todos los pares en un solo stream (más eficiente).
    Formato: <symbol>@kline_<interval>
    """
    streams = "/".join(
        f"{sym.lower()}@kline_{KLINE_INTERVAL}"
        for sym in SYMBOLS
    )
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


async def listen():
    url = build_ws_url()
    log.info(f"Conectando WebSocket: {len(SYMBOLS)} pares | {KLINE_INTERVAL}")
    log.info(f"Pares: {', '.join(SYMBOLS)}")

    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=30) as ws:
                log.info("✅ WebSocket conectado")
                reconnect_delay = 5  # reset

                async for raw_msg in ws:
                    msg = json.loads(raw_msg)

                    # Stream combinado: { "stream": "btcusdt@kline_1m", "data": {...} }
                    if "data" not in msg:
                        continue

                    data   = msg["data"]
                    symbol = data["s"]          # ej: "BTCUSDT"
                    kline  = data["k"]

                    if symbol in candle_buffer:
                        process_kline(symbol, kline)

        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"WebSocket cerrado: {e}. Reconectando en {reconnect_delay}s...")
        except Exception as e:
            log.error(f"Error WebSocket: {e}. Reconectando en {reconnect_delay}s...")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)  # backoff exponencial


# ============================================================
#  Monitor periódico (log de estado cada 5 min)
# ============================================================
async def status_monitor():
    while True:
        await asyncio.sleep(30)  # actualiza balance cada 30s
        n_pos = len(open_positions)
        buffers_ok = sum(1 for b in candle_buffer.values() if len(b) >= 30)
        log.info(
            f"[STATUS] {datetime.utcnow().strftime('%H:%M:%S')} UTC | "
            f"Posiciones abiertas: {n_pos} | "
            f"Pares con datos: {buffers_ok}/{len(SYMBOLS)}"
        )
        for sym, pos in open_positions.items():
            log.info(
                f"  → {sym} entry:{pos['entry_price']:.4f} "
                f"SL:{pos['stop_loss']:.4f} TP:{pos['take_profit']:.4f}"
            )
        # Sincronizar estado al dashboard
        dash.update_state("open_positions", dict(open_positions))
        dash.update_state("candle_buffer", dict(candle_buffer))
        dash.update_state("balance_usdt", get_balance("USDT"))


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
    log.info("  SCALPING BOT — Binance Testnet Spot")
    log.info(f"  Pares: {len(SYMBOLS)} | Intervalo: {KLINE_INTERVAL}")
    log.info(f"  SL: -1.5% | TP: +2.5% | Max pos: 3")
    log.info("=" * 55)

    # Inicializar buffer en dashboard
    dash.update_state("candle_buffer", dict(candle_buffer))

    # Arrancar servidor web del dashboard
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

    # Capturar Ctrl+C para cierre limpio
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_shutdown, loop)
        except NotImplementedError:
            pass  # Windows no soporta add_signal_handler

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        handle_shutdown(loop)
    finally:
        loop.close()
