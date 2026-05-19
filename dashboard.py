# ============================================================
#  MÓDULO: dashboard.py  v2
#  Flask API + semáforo de activos + posiciones con color
# ============================================================

import threading
import time
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory, request
import os
import csv

app = Flask(__name__, static_folder="static")

_state = {
    "open_positions": {},
    "candle_buffer":  {},
    "last_signals":   [],
    "semaphore":      {},   # { symbol: { color, rsi, estado } }
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
            "time":          datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "symbol":        symbol,
            "action":        action,
            "confirmations": confirmations,
            "price":         round(price, 4),
            "rsi":           indicators.get("rsi", "—"),
            "macd":          indicators.get("macd", "—"),
            "ema":           indicators.get("ema", "—"),
        })
        _state["last_signals"] = _state["last_signals"][:50]


def push_semaphore(symbol: str, action: str, rsi: float, estado: str, confirmations: int):
    """
    Actualiza el semáforo de cada par.
    Verde  → BUY disparado
    Amarillo → RSI_OK_ESPERANDO o ACERCANDOSE
    Rojo   → sobrecomprado o NEUTRO sin señal
    """
    if action == "BUY":
        color = "green"
    elif action == "SELL":
        color = "red"
    elif estado in ("RSI_OK_ESPERANDO", "ACERCANDOSE"):
        color = "yellow"
    else:
        color = "gray"

    with _lock:
        _state["semaphore"][symbol] = {
            "color":  color,
            "rsi":    round(rsi, 1),
            "estado": estado,
            "conf":   confirmations,
        }


# --- CSV helpers ---
def read_trade_history(limit=20):
    trades = []
    if not os.path.exists("trades.csv"):
        return trades
    try:
        with open("trades.csv", "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            for row in reversed(rows[-limit:]):
                trades.append(row)
    except Exception:
        pass
    return trades


def calc_daily_pnl():
    if not os.path.exists("trades.csv"):
        return 0.0, 0, 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_pnl = 0.0
    wins = losses = 0
    try:
        with open("trades.csv", "r") as f:
            reader = csv.DictReader(f)
            buys = {}
            for row in reader:
                if not row["timestamp"].startswith(today):
                    continue
                sym    = row["symbol"]
                action = row["action"]
                price  = float(row["price"])
                if action == "BUY":
                    buys[sym] = price
                elif action.startswith("SELL") and sym in buys:
                    pnl = ((price - buys[sym]) / buys[sym]) * 100
                    total_pnl += pnl
                    wins   += 1 if pnl >= 0 else 0
                    losses += 1 if pnl <  0 else 0
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
        positions  = dict(_state["open_positions"])
        signals    = list(_state["last_signals"])
        balance    = _state["balance_usdt"]
        started    = _state["started_at"]
        semaphore  = dict(_state["semaphore"])
        buf        = dict(_state["candle_buffer"])

    # Posiciones con P&L flotante y color
    pos_list = []
    for sym, pos in positions.items():
        b = buf.get(sym)
        current = list(b)[-1]["close"] if b and len(b) > 0 else pos["entry_price"]
        pnl_pct = ((current - pos["entry_price"]) / pos["entry_price"]) * 100
        pos_color = "green" if pnl_pct > 0.3 else ("red" if pnl_pct < -0.5 else "yellow")
        pos_list.append({
            "symbol":        sym,
            "entry_price":   round(pos["entry_price"], 4),
            "current_price": round(current, 4),
            "quantity":      round(pos["quantity"], 6),
            "stop_loss":     round(pos["stop_loss"], 4),
            "take_profit":   round(pos["take_profit"], 4),
            "pnl_pct":       round(pnl_pct, 2),
            "color":         pos_color,
        })

    pnl_day, wins, losses = calc_daily_pnl()
    trades = read_trade_history(20)
    uptime = round((datetime.now(timezone.utc) -
                    datetime.fromisoformat(started)).total_seconds() / 60, 1)

    return jsonify({
        "status":          "running",
        "started_at":      started,
        "uptime_min":      uptime,
        "balance_usdt":    round(balance, 2),
        "open_positions":  pos_list,
        "last_signals":    signals[:15],
        "pnl_day_pct":     pnl_day,
        "wins_today":      wins,
        "losses_today":    losses,
        "trade_history":   trades,
        "monitored_pairs": len(buf),
        "semaphore":       semaphore,
    })


@app.route("/api/prices")
def api_prices():
    prices = {}
    with _lock:
        for sym, b in _state["candle_buffer"].items():
            if b and len(b) > 0:
                prices[sym] = round(list(b)[-1]["close"], 4)
    return jsonify(prices)


@app.route("/api/force_sell/<symbol>", methods=["POST"])
def api_force_sell(symbol):
    """Venta forzada manual de una posición específica."""
    try:
        from executor import force_sell
        sym = symbol.upper()
        from executor import open_positions
        if sym not in open_positions:
            return jsonify({"ok": False, "message": f"{sym} ya fue vendido (trailing/SL/TP)"}), 400
        success = force_sell(sym)
        if success:
            update_state("open_positions", dict(open_positions))
            return jsonify({"ok": True, "message": f"✅ {sym} vendido al mercado"})
        return jsonify({"ok": False, "message": f"Error ejecutando orden de venta para {sym}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/sync_positions", methods=["POST"])
def api_sync_positions():
    """Sincroniza posiciones con Binance en tiempo real."""
    try:
        from executor import recover_positions_from_binance, open_positions
        n = recover_positions_from_binance()
        update_state("open_positions", dict(open_positions))
        return jsonify({"ok": True, "message": f"Sincronizado — {n} posiciones activas"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/force_sell_all", methods=["POST"])
def api_force_sell_all():
    """Cierra todas las posiciones abiertas."""
    try:
        from executor import force_close_all
        count = force_close_all()
        return jsonify({"ok": True, "message": f"{count} posiciones cerradas"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


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
