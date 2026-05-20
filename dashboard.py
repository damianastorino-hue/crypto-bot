# ============================================================
#  SCALPING BOT — dashboard.py  v4
#  Flask API + estado bot + P&L diario
# ============================================================

import csv
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, request, send_from_directory

from config import MAX_OPEN_POSITIONS

app = Flask(__name__, static_folder="static")

_state = {
    "open_positions": {},
    "candle_buffer":  {},
    "last_signals":   [],
    "semaphore":      {},
    "balance_usdt":   0.0,
    "started_at":     datetime.now(timezone.utc).isoformat(),
    "bot_active":     True,
    "buying_paused":  False,
}

_lock = threading.Lock()


# ============================================================
#  Helpers de estado
# ============================================================
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
    color = (
        "green"  if action == "BUY"  else
        "red"    if action == "SELL" else
        "yellow" if estado in ("RSI_OK_ESPERANDO", "ACERCANDOSE") else
        "gray"
    )
    with _lock:
        _state["semaphore"][symbol] = {
            "color": color, "rsi": round(rsi, 1),
            "estado": estado, "conf": confirmations,
        }


# ============================================================
#  P&L diario — ventana 0:00-23:59 hora Argentina (UTC-3)
#  Ganancia en USD absolutos + porcentaje sobre capital real
# ============================================================
def calc_daily_pnl() -> tuple[float, int, int, float, float]:
    """
    Retorna (pnl_pct, wins, losses, ganancia_usd, capital_total).
    - ganancia_usd: suma de (precio_venta - precio_compra) * cantidad
    - capital_total: USDT libre + valor actual de posiciones abiertas
    - pnl_pct: ganancia_usd / capital_total * 100
    Ventana: desde las 00:00:00 hora Argentina del día actual.
    """
    now_arg   = datetime.now(timezone.utc) - timedelta(hours=3)
    today_arg = now_arg.strftime("%Y-%m-%d")

    ganancia_usd = 0.0
    wins = losses = 0

    log_file = os.environ.get("LOG_FILE", "trades.csv")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                buys = {}
                for row in csv.DictReader(f):
                    try:
                        ts_utc = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                        ts_arg = ts_utc.replace(tzinfo=timezone.utc) - timedelta(hours=3)
                        if ts_arg.strftime("%Y-%m-%d") != today_arg:
                            continue
                    except Exception:
                        continue
                    sym    = row["symbol"]
                    action = row["action"]
                    price  = float(row["price"])
                    qty    = float(row.get("quantity", 0) or 0)
                    if action == "BUY":
                        buys[sym] = {"price": price, "qty": qty}
                    elif action.startswith("SELL") and sym in buys:
                        g = (price - buys[sym]["price"]) * buys[sym]["qty"]
                        ganancia_usd += g
                        wins         += 1 if g >= 0 else 0
                        losses       += 1 if g <  0 else 0
                        del buys[sym]
        except Exception:
            pass

    # Capital total = USDT libre + valor de mercado de posiciones abiertas
    with _lock:
        balance   = _state["balance_usdt"]
        positions = dict(_state["open_positions"])
        buf       = dict(_state["candle_buffer"])

    capital_total = balance
    for sym, pos in positions.items():
        b = buf.get(sym)
        price = list(b)[-1]["close"] if b and len(b) > 0 else pos["entry_price"]
        capital_total += pos["quantity"] * price

    pnl_pct = (ganancia_usd / capital_total * 100) if capital_total > 0 else 0.0
    return round(pnl_pct, 2), wins, losses, round(ganancia_usd, 4), round(capital_total, 2)


def _read_trade_history(limit: int = 20) -> list[dict]:
    log_file = os.environ.get("LOG_FILE", "trades.csv")
    trades = []
    if not os.path.exists(log_file):
        return trades
    try:
        with open(log_file, "r") as f:
            rows = list(csv.DictReader(f))
            trades = list(reversed(rows[-limit:]))
    except Exception:
        pass
    return trades


# ============================================================
#  Endpoints
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
        b       = buf.get(sym)
        current = list(b)[-1]["close"] if b and len(b) > 0 else pos["entry_price"]
        pnl_pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
        pos_list.append({
            "symbol":        sym,
            "entry_price":   round(pos["entry_price"], 6),
            "current_price": round(current, 6),
            "quantity":      round(pos["quantity"], 6),
            "stop_loss":     round(pos["stop_loss"], 6),
            "take_profit":   round(pos["take_profit"], 6),
            "pnl_pct":       round(pnl_pct, 2),
            "color":         "green" if pnl_pct > 0.3 else ("red" if pnl_pct < -0.5 else "yellow"),
        })

    pnl_pct, wins, losses, ganancia_usdt, capital = calc_daily_pnl()
    uptime = round(
        (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 60, 1
    )

    return jsonify({
        "status":          "running",
        "started_at":      started,
        "uptime_min":      uptime,
        "balance_usdt":    round(balance, 2),
        "open_positions":  pos_list,
        "last_signals":    signals[:15],
        "pnl_day_pct":     pnl_pct,
        "wins_today":      wins,
        "losses_today":    losses,
        "ganancia_usdt":   ganancia_usdt,
        "capital_total":   capital,
        "trade_history":   _read_trade_history(20),
        "monitored_pairs": len(buf),
        "semaphore":       semaphore,
        "bot_active":      bot_active,
        "buying_paused":   buying_paused,
        "max_positions":   MAX_OPEN_POSITIONS,
    })


@app.route("/api/pnl")
def api_pnl():
    """Endpoint separado de P&L — el dashboard lo llama cada 10s."""
    pnl_pct, wins, losses, ganancia_usdt, capital = calc_daily_pnl()
    return jsonify({
        "pnl_pct":      pnl_pct,
        "wins":         wins,
        "losses":       losses,
        "ganancia_usdt": ganancia_usdt,
        "capital_total": capital,
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
    action = (request.get_json() or {}).get("action", "")
    with _lock:
        if action == "stop":
            _state["bot_active"]    = False
            _state["buying_paused"] = False
            msg = "Bot DETENIDO"
        elif action == "pause_buying":
            _state["bot_active"]    = True
            _state["buying_paused"] = True
            msg = "Compras PAUSADAS"
        elif action == "start":
            _state["bot_active"]    = True
            _state["buying_paused"] = False
            msg = "Bot ACTIVO"
        else:
            return jsonify({"ok": False, "message": "Acción inválida"}), 400
        bot_active    = _state["bot_active"]
        buying_paused = _state["buying_paused"]
    return jsonify({"ok": True, "message": msg,
                    "bot_active": bot_active, "buying_paused": buying_paused})


@app.route("/api/toggle_bot", methods=["POST"])
def api_toggle_bot():
    """Compatibilidad con dashboard.html (toggleBot usa /api/toggle_bot)."""
    with _lock:
        currently_active = _state.get("bot_active", True)
        _state["bot_active"] = not currently_active
        msg = "Bot ACTIVO" if _state["bot_active"] else "Bot DETENIDO"
        active = _state["bot_active"]
    return jsonify({"ok": True, "message": msg, "bot_active": active})


@app.route("/api/force_sell/<symbol>", methods=["POST"])
def api_force_sell(symbol):
    sym = symbol.upper()
    try:
        from executor import open_positions, force_sell
        if sym not in open_positions:
            return jsonify({"ok": False, "message": f"{sym} no está en posiciones"}), 400
        if force_sell(sym):
            update_state("open_positions", dict(open_positions))
            return jsonify({"ok": True, "message": f"✅ {sym} vendido"})
        return jsonify({"ok": False, "message": f"Error al vender {sym}"}), 500
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


@app.route("/api/db/signals")
def api_db_signals():
    limit  = int(request.args.get("limit", 200))
    symbol = request.args.get("symbol")
    try:
        import database as db
        with db.get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                    (symbol.upper(), limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM signals ORDER BY ts DESC LIMIT ?",
                    (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/db/trades")
def api_db_trades():
    limit = int(request.args.get("limit", 100))
    try:
        import database as db
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/db/stats")
def api_db_stats():
    try:
        import database as db
        with db.get_conn() as conn:
            n_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            n_trades  = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            n_candles = conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            first_ts  = conn.execute("SELECT MIN(ts) FROM candles").fetchone()[0]
            last_ts   = conn.execute("SELECT MAX(ts) FROM candles").fetchone()[0]
            wins      = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE action LIKE 'SELL%' AND pnl_pct > 0"
            ).fetchone()[0]
            losses    = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE action LIKE 'SELL%' AND pnl_pct <= 0"
            ).fetchone()[0]
            avg_pnl   = conn.execute(
                "SELECT AVG(pnl_pct) FROM trades WHERE action LIKE 'SELL%'"
            ).fetchone()[0]
        total = wins + losses
        return jsonify({
            "signals_total": n_signals,
            "trades_total":  n_trades,
            "candles_total": n_candles,
            "candles_desde": first_ts,
            "candles_hasta": last_ts,
            "wins":          wins,
            "losses":        losses,
            "win_rate_pct":  round(wins / total * 100, 1) if total > 0 else 0,
            "avg_pnl_pct":   round(avg_pnl, 3) if avg_pnl else 0,
            "db_path":       db.DB_FILE,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory("static", "dashboard.html")


# ============================================================
#  Arranque en hilo daemon
# ============================================================
_ready = threading.Event()


def _run_flask(host, port):
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
    _ready.set()
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def start_dashboard(host: str = "0.0.0.0", port: int = 5000):
    t = threading.Thread(target=_run_flask, args=(host, port), daemon=True)
    t.start()
    _ready.wait(timeout=10)
    time.sleep(0.5)
    return t
