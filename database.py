# ============================================================
#  SCALPING BOT — database.py  v2
#  SQLite: candles, signals, trades, heartbeat
# ============================================================

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from logger import log

DB_FILE = os.environ.get("DB_PATH", "scalping.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crea las tablas si no existen."""
    with get_conn() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                close         REAL,
                rsi           REAL,
                macd          REAL,
                ema_fast      REAL,
                ema_slow      REAL,
                bb_upper      REAL,
                bb_lower      REAL,
                action        TEXT,
                confirmations INTEGER
            );

            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                action      TEXT NOT NULL,
                price       REAL,
                quantity    REAL,
                amount_usdt REAL,
                stop_loss   REAL,
                take_profit REAL,
                pnl_pct     REAL,
                reason      TEXT
            );

            CREATE TABLE IF NOT EXISTS candles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                symbol  TEXT NOT NULL,
                open    REAL,
                high    REAL,
                low     REAL,
                close   REAL,
                volume  REAL
            );

            CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON candles(symbol, ts);

            CREATE TABLE IF NOT EXISTS daily_summary (
                date         TEXT PRIMARY KEY,
                total_trades INTEGER,
                wins         INTEGER,
                losses       INTEGER,
                pnl_pct      REAL,
                best_pair    TEXT,
                worst_pair   TEXT,
                fees_usdt    REAL
            );

            CREATE TABLE IF NOT EXISTS heartbeat (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                last_ts TEXT NOT NULL
            );
        ''')
    log.info("✅ SQLite inicializado")


# ============================================================
#  HEARTBEAT
# ============================================================
def update_heartbeat():
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO heartbeat (id, last_ts) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_ts=excluded.last_ts",
            (now,),
        )


def get_last_heartbeat() -> datetime | None:
    with get_conn() as conn:
        row = conn.execute("SELECT last_ts FROM heartbeat WHERE id=1").fetchone()
    if not row:
        return None
    ts = datetime.fromisoformat(row["last_ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def was_recently_stopped(max_minutes: int = 5) -> bool:
    last = get_last_heartbeat()
    if not last:
        return False
    diff = (datetime.now(timezone.utc) - last).total_seconds() / 60
    log.info(f"Último heartbeat: hace {diff:.1f} minutos")
    return diff <= max_minutes


# ============================================================
#  VELAS
# ============================================================
def save_candle(symbol: str, candle: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO candles (ts, symbol, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(), symbol,
                candle["open"], candle["high"], candle["low"],
                candle["close"], candle["volume"],
            ),
        )


def load_recent_candles(symbol: str, limit: int = 100) -> list[dict]:
    """Retorna las últimas `limit` velas en orden cronológico."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT open, high, low, close, volume "
            "FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def clean_old_candles(days: int = 3):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM candles WHERE ts < ?", (cutoff,))


# ============================================================
#  SEÑALES
# ============================================================
def save_signal(symbol: str, ind: dict, action: str, confirmations: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO signals "
            "(ts, symbol, close, rsi, macd, ema_fast, ema_slow, "
            " bb_upper, bb_lower, action, confirmations) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(), symbol,
                ind.get("close"), ind.get("rsi"),   ind.get("macd"),
                ind.get("ema_fast"), ind.get("ema_slow"),
                ind.get("bb_upper"), ind.get("bb_lower"),
                action, confirmations,
            ),
        )


# ============================================================
#  TRADES
# ============================================================
def save_trade(symbol: str, action: str, price: float, quantity: float,
               amount_usdt: float, stop_loss: float, take_profit: float,
               pnl_pct: float = 0.0, reason: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO trades "
            "(ts, symbol, action, price, quantity, amount_usdt, "
            " stop_loss, take_profit, pnl_pct, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                symbol, action, price, quantity, amount_usdt,
                stop_loss, take_profit, pnl_pct, reason,
            ),
        )


def get_daily_stats() -> dict:
    """P&L del día actual (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE ts LIKE ? ORDER BY ts",
            (f"{today}%",),
        ).fetchall()

    buys      = {}
    wins      = losses = 0
    total_pnl = 0.0
    pair_pnl  = {}

    for row in rows:
        sym = row["symbol"]
        if row["action"] == "BUY":
            buys[sym] = row["price"]
        elif row["action"].startswith("SELL") and sym in buys:
            pnl = ((row["price"] - buys[sym]) / buys[sym]) * 100
            total_pnl         += pnl
            pair_pnl[sym]      = pair_pnl.get(sym, 0) + pnl
            wins              += 1 if pnl >= 0 else 0
            losses            += 1 if pnl <  0 else 0
            del buys[sym]

    return {
        "date":         today,
        "total_trades": wins + losses,
        "wins":         wins,
        "losses":       losses,
        "pnl_pct":      round(total_pnl, 2),
        "best_pair":    max(pair_pnl, key=pair_pnl.get) if pair_pnl else "—",
        "worst_pair":   min(pair_pnl, key=pair_pnl.get) if pair_pnl else "—",
    }
