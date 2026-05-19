# ============================================================
#  MÓDULO: dashboard.py  v3
#  Bot control ON/OFF/PAUSE + P&L correcto + capital dinámico
# ============================================================

import threading
import time
import os
import csv
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request

app = Flask(__name__, static_folder="static")

_state = {
    "open_positions": {},
    "candle_buffer":  {},
    "last_signals":   [],
    "semaphore":      {},
    "balance_usdt":   0.0,
    "started_at":     datetime.now(timezone.utc).isoformat(),
    "bot_active":     True,   # False = detenido (sin compras ni ventas auto)
    "buying_paused":  False,  # True = solo seguimiento, sin nuevas compras
}

_lock = threading.Lock()


def update_state(key, value):
    with _lock:
        _state[key] = value

def is_bot_active() -> bool:
    with _lock:
        return _state.get("bot_active", True)

def is_buying_paused() -> bool:
    with _lock:
        return _state.get("buying_paused", False)

def push_signal(symbol, action, confirmations, indicators, price):
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

def push_semaphore(symbol, action, rsi, estado, confirmations):
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
            "color": color, "rsi": round(rsi, 1),
            "estado": estado, "conf": confirmations,
        }


# ============================================================
#  P&L DIARIO — hora Argentina (UTC-3), capital total real
# ============================================================
def calc_daily_pnl():
    now_arg   = datetime.now(timezone.utc) - timedelta(hours=3)
    today_arg = now_arg.strftime("%Y-%m-%d")
    total_ganancia = 0.0
    wins = losses = 0

    if os.path.exists("trades.csv"):
        try:
            with open("trades.csv", "r") as f:
                reader = csv.DictReader(f)
                buys = {}
                for row in reader:
                    try:
                        ts_utc = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                        ts_arg = ts_utc - timedelta(hours=3)
                        if ts_arg.strftime("%Y-%m-%d") != today_arg:
                            continue
                    except Exception:
                        continue
                    sym    = row["symbol"]
                    action = row["action"]
                    price  = float(row["price"])
                    qty    = float(row.get("quantity", 0))
                    if action == "BUY":
                        buys[sym] = {"price": price, "qty": qty}
                    elif action.startswith("SELL") and sym in buys:
                        ganancia = (price - buys[sym]["price"]) * buys[sym]["qty"]
                        total_ganancia += ganancia
                        wins   += 1 if ganancia >= 0 else 0
                        losses += 1 if ganancia <  0 else 0
                        del buys[sym]
        except Exception:
            pass

    # Capital total = USDT libre + valor actual posiciones abiertas
    with _lock:
        balance  = _state["balance_usdt"]
        positions = dict(_state["open_positions"])
        buf       = dict(_state["candle_buffer"])

    capital_total = balance
    for sym, pos in positions.items():
        b = buf.get(sym)
        price = list(b)[-1]["close"] if b and len(b) > 0 else pos["entry_price"]
        capital_total += pos["quantity"] * price

    pnl_pct = (total_ganancia / capital_total * 100) if capital_total > 0 else 0.0
    return round(pnl_pct, 2), wins, losses


def read_trade_history(limit=20):
    trades = []
    if not os.path.exists("trades.csv"):
        return trades
    try:
        with open("trades.csv", "r") as f:
            rows = list(csv.DictReader(f))
            for row in reversed(rows[-limit:]):
                trades.append(row)
    except Exception:
        pass
    return trades


# ============================================================
#  API ENDPOINTS
# ============================================================

@app.route("/api/status")
def api_status():
    with _lock:
        positions     = dict(_state["open_positions"])
        signals       = list(_state["last_signals"])
        balance       = _state["balance_usdt"]
        started       = _state["started_at"]
        semaphore     = dict(_state["semaphore"])
        buf           = dict(_state["candle_buffer"])
        bot_active    = _state.get("bot_active", True)
        buying_paused = _state.get("buying_paused", False)

    pos_list = []
    for sym, pos in positions.items():
        b = buf.get(sym)
        current = list(b)[-1]["close"] if b and len(b) > 0 else pos["entry_price"]
        pnl_pct  = ((current - pos["entry_price"]) / pos["entry_price"]) * 100
        pos_color = "green" if pnl_pct > 0.3 else ("red" if pnl_pct < -0.5 else "yellow")
        pos_list.append({
            "symbol":        sym,
            "entry_price":   round(pos["entry_price"], 6),
            "current_price": round(current, 6),
            "quantity":      round(pos["quantity"], 6),
            "stop_loss":     round(pos["stop_loss"], 6),
            "take_profit":   round(pos["take_profit"], 6),
            "pnl_pct":       round(pnl_pct, 2),
            "color":         pos_color,
        })

    pnl_day, wins, losses = calc_daily_pnl()
    trades  = read_trade_history(20)
    uptime  = round((datetime.now(timezone.utc) -
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
        "bot_active":      bot_active,
        "buying_paused":   buying_paused,
    })


@app.route("/api/prices")
def api_prices():
    prices = {}
    with _lock:
        for sym, b in _state["candle_buffer"].items():
            if b and len(b) > 0:
                prices[sym] = round(list(b)[-1]["close"], 6)
    return jsonify(prices)


@app.route("/api/bot_control", methods=["POST"])
def api_bot_control():
    data   = request.get_json() or {}
    action = data.get("action", "")
    with _lock:
        if action == "stop":
            _state["bot_active"]    = False
            _state["buying_paused"] = False
            msg = "Bot DETENIDO — sin operaciones automáticas"
        elif action == "pause_buying":
            _state["bot_active"]    = True
            _state["buying_paused"] = True
            msg = "Compras PAUSADAS — siguiendo posiciones abiertas"
        elif action == "start":
            _state["bot_active"]    = True
            _state["buying_paused"] = False
            msg = "Bot ACTIVO — operando normalmente"
        else:
            return jsonify({"ok": False, "message": "Acción inválida"}), 400
    return jsonify({"ok": True, "message": msg,
                    "bot_active":    _state["bot_active"],
                    "buying_paused": _state["buying_paused"]})


@app.route("/api/force_sell/<symbol>", methods=["POST"])
def api_force_sell(symbol):
    try:
        sym = symbol.upper()
        from executor import open_positions, force_sell
        if sym not in open_positions:
            return jsonify({"ok": False, "message": f"{sym} ya fue vendido"}), 400
        success = force_sell(sym)
        if success:
            update_state("open_positions", dict(open_positions))
            return jsonify({"ok": True, "message": f"✅ {sym} vendido al mercado"})
        return jsonify({"ok": False, "message": f"Error ejecutando orden para {sym}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/force_sell_all", methods=["POST"])
def api_force_sell_all():
    try:
        from executor import force_close_all
        count = force_close_all()
        return jsonify({"ok": True, "message": f"{count} posiciones cerradas"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/sync_positions", methods=["POST"])
def api_sync_positions():
    try:
        from executor import recover_positions_from_binance, open_positions
        n = recover_positions_from_binance()
        update_state("open_positions", dict(open_positions))
        return jsonify({"ok": True, "message": f"Sincronizado — {n} posiciones activas"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


# ============================================================
#  Arranque
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
