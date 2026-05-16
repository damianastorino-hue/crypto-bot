# ============================================================
#  MÓDULO: dashboard.py
#  Servidor Flask — API de estado + sirve el dashboard HTML
#  Corre en paralelo al bot (hilo separado)
# ============================================================

import threading
import time
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory
import os
import csv

app = Flask(__name__, static_folder="static")

# Referencia al estado compartido del bot (inyectado desde bot.py)
_state = {
    "open_positions": {},   # { symbol: { entry_price, quantity, stop_loss, take_profit } }
    "candle_buffer":  {},   # { symbol: deque }
    "last_signals":   [],   # últimas 50 señales [ {time, symbol, action, conf, indicators} ]
    "balance_usdt":   0.0,
    "started_at":     datetime.now(timezone.utc).isoformat(),
}

_lock = threading.Lock()


def update_state(key, value):
    with _lock:
        _state[key] = value


def push_signal(symbol: str, action: str, confirmations: int, indicators: dict, price: float):
    with _lock:
        _state["last_signals"].insert(0, {
            "time":         datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "symbol":       symbol,
            "action":       action,
            "confirmations": confirmations,
            "price":        round(price, 4),
            "rsi":          indicators.get("rsi", "—"),
            "macd":         indicators.get("macd", "—"),
            "ema":          indicators.get("ema", "—"),
        })
        # Mantener solo las últimas 50
        _state["last_signals"] = _state["last_signals"][:50]


# --- Leer historial CSV ---
def read_trade_history(limit=20):
    trades = []
    log_file = "trades.csv"
    if not os.path.exists(log_file):
        return trades
    try:
        with open(log_file, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            for row in reversed(rows[-limit:]):
                trades.append(row)
    except Exception:
        pass
    return trades


# --- Calcular P&L del día ---
def calc_daily_pnl():
    log_file = "trades.csv"
    if not os.path.exists(log_file):
        return 0.0, 0, 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_pnl = 0.0
    wins = 0
    losses = 0
    try:
        with open(log_file, "r") as f:
            reader = csv.DictReader(f)
            buys = {}
            for row in reader:
                if not row["timestamp"].startswith(today):
                    continue
                sym = row["symbol"]
                action = row["action"]
                price = float(row["price"])
                if action == "BUY":
                    buys[sym] = price
                elif action.startswith("SELL") and sym in buys:
                    pnl = ((price - buys[sym]) / buys[sym]) * 100
                    total_pnl += pnl
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1
                    del buys[sym]
    except Exception:
        pass
    return round(total_pnl, 2), wins, losses


# ============================================================
#  API ENDPOINTS
# ============================================================

@app.route("/api/status")
def api_status():
    with _lock:
        positions = dict(_state["open_positions"])
        signals   = list(_state["last_signals"])
        balance   = _state["balance_usdt"]
        started   = _state["started_at"]

    # Enriquecer posiciones con P&L flotante (necesita precio actual)
    pos_list = []
    for sym, pos in positions.items():
        # precio actual: última vela del buffer
        buf = _state["candle_buffer"].get(sym)
        current_price = list(buf)[-1]["close"] if buf and len(buf) > 0 else pos["entry_price"]
        pnl_pct = ((current_price - pos["entry_price"]) / pos["entry_price"]) * 100
        pos_list.append({
            "symbol":       sym,
            "entry_price":  round(pos["entry_price"], 4),
            "current_price": round(current_price, 4),
            "quantity":     round(pos["quantity"], 6),
            "stop_loss":    round(pos["stop_loss"], 4),
            "take_profit":  round(pos["take_profit"], 4),
            "pnl_pct":      round(pnl_pct, 2),
        })

    pnl_day, wins, losses = calc_daily_pnl()
    trades = read_trade_history(20)

    return jsonify({
        "status":           "running",
        "started_at":       started,
        "uptime_min":       round((datetime.now(timezone.utc) -
                             datetime.fromisoformat(started)).total_seconds() / 60, 1),
        "balance_usdt":     round(balance, 2),
        "open_positions":   pos_list,
        "last_signals":     signals[:15],
        "pnl_day_pct":      pnl_day,
        "wins_today":       wins,
        "losses_today":     losses,
        "trade_history":    trades,
        "monitored_pairs":  len(_state["candle_buffer"]),
    })


@app.route("/api/prices")
def api_prices():
    """Últimos precios de cierre de cada par."""
    prices = {}
    with _lock:
        for sym, buf in _state["candle_buffer"].items():
            if buf and len(buf) > 0:
                prices[sym] = round(list(buf)[-1]["close"], 4)
    return jsonify(prices)


@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


# ============================================================
#  Arranque en hilo separado
# ============================================================
_ready = threading.Event()

def _run_flask(host, port):
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    _ready.set()
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

def start_dashboard(host="0.0.0.0", port=5000):
    t = threading.Thread(target=_run_flask, args=(host, port), daemon=True)
    t.start()
    _ready.wait(timeout=10)
    time.sleep(1)
    return t
