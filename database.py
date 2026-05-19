# ============================================================
#  MÓDULO: database.py
#  SQLite: signals, trades, daily_summary + reinicio inteligente
# ============================================================

import sqlite3
import os
import json
from datetime import datetime, timezone, timedelta
from logger import log

import os
DB_FILE = os.environ.get("DB_PATH", "scalping.db")


def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crea las tablas si no existen."""
    conn = get_conn()
    c = conn.cursor()

    # Tabla de señales — cada lectura de indicadores por par por minuto
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        close       REAL,
        rsi         REAL,
        macd        REAL,
        ema_fast    REAL,
        ema_slow    REAL,
        bb_upper    REAL,
        bb_lower    REAL,
        action      TEXT,
        confirmations INTEGER
    )''')

    # Tabla de trades ejecutados
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
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
    )''')

    # Tabla de velas OHLCV — para reinicio inteligente
    c.execute('''CREATE TABLE IF NOT EXISTS candles (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        volume      REAL
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON candles(symbol, ts)')

    # Tabla resumen diario
    c.execute('''CREATE TABLE IF NOT EXISTS daily_summary (
        date        TEXT PRIMARY KEY,
        total_trades INTEGER,
        wins        INTEGER,
        losses      INTEGER,
        pnl_pct     REAL,
        best_pair   TEXT,
        worst_pair  TEXT,
        fees_usdt   REAL
    )''')

    # Tabla heartbeat — para reinicio inteligente
    c.execute('''CREATE TABLE IF NOT EXISTS heartbeat (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        last_ts TEXT NOT NULL
    )''')

    conn.commit()
    conn.close()
    log.info("✅ SQLite inicializado")


# ============================================================
#  HEARTBEAT — saber cuándo se detuvo el bot
# ============================================================
def update_heartbeat():
    """Actualizar cada 30s para saber cuándo fue el último run."""
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''INSERT INTO heartbeat (id, last_ts) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET last_ts=excluded.last_ts''', (now,))
    conn.commit()
    conn.close()


def get_last_heartbeat() -> datetime | None:
    """Retorna el último heartbeat registrado."""
    conn = get_conn()
    row = conn.execute('SELECT last_ts FROM heartbeat WHERE id=1').fetchone()
    conn.close()
    if row:
        return datetime.fromisoformat(row['last_ts'])
    return None


def was_recently_stopped(max_minutes: int = 5) -> bool:
    """
    Retorna True si el bot estuvo offline menos de max_minutes.
    Usado para el reinicio inteligente.
    """
    last = get_last_heartbeat()
    if not last:
        return False
    now = datetime.now(timezone.utc)
    # Asegurar que last tiene timezone
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    diff = (now - last).total_seconds() / 60
    log.info(f"Último heartbeat: hace {diff:.1f} minutos")
    return diff <= max_minutes


# ============================================================
#  VELAS — guardar y recuperar para reinicio inteligente
# ============================================================
def save_candle(symbol: str, candle: dict):
    """Guarda una vela cerrada en la DB."""
    conn = get_conn()
    conn.execute('''INSERT INTO candles (ts, symbol, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''', (
        datetime.now(timezone.utc).isoformat(),
        symbol,
        candle['open'], candle['high'], candle['low'],
        candle['close'], candle['volume']
    ))
    conn.commit()
    conn.close()


def load_recent_candles(symbol: str, limit: int = 100) -> list[dict]:
    """
    Carga las últimas `limit` velas de un par desde la DB.
    Retorna lista en formato OHLCV compatible con compute_indicators.
    """
    conn = get_conn()
    rows = conn.execute('''SELECT open, high, low, close, volume
                           FROM candles WHERE symbol=?
                           ORDER BY ts DESC LIMIT ?''',
                        (symbol, limit)).fetchall()
    conn.close()
    # Invertir para orden cronológico
    return [dict(r) for r in reversed(rows)]


def clean_old_candles(days: int = 60):
    """Borrar velas de más de X días para no crecer indefinidamente."""
    conn = get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute('DELETE FROM candles WHERE ts < ?', (cutoff,))
    conn.commit()
    conn.close()


# ============================================================
#  SEÑALES
# ============================================================
def save_signal(symbol: str, ind: dict, action: str, confirmations: int):
    """Guarda una lectura de indicadores."""
    conn = get_conn()
    conn.execute('''INSERT INTO signals
        (ts, symbol, close, rsi, macd, ema_fast, ema_slow, bb_upper, bb_lower, action, confirmations)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
        datetime.now(timezone.utc).isoformat(),
        symbol,
        ind.get('close'), ind.get('rsi'), ind.get('macd'),
        ind.get('ema_fast'), ind.get('ema_slow'),
        ind.get('bb_upper'), ind.get('bb_lower'),
        action, confirmations
    ))
    conn.commit()
    conn.close()


# ============================================================
#  TRADES
# ============================================================
def save_trade(symbol: str, action: str, price: float, quantity: float,
               amount_usdt: float, stop_loss: float, take_profit: float,
               pnl_pct: float = 0.0, reason: str = ""):
    conn = get_conn()
    conn.execute('''INSERT INTO trades
        (ts, symbol, action, price, quantity, amount_usdt, stop_loss, take_profit, pnl_pct, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
        datetime.now(timezone.utc).isoformat(),
        symbol, action, price, quantity, amount_usdt,
        stop_loss, take_profit, pnl_pct, reason
    ))
    conn.commit()
    conn.close()


def get_daily_stats() -> dict:
    """Calcula P&L del día actual."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn  = get_conn()
    rows  = conn.execute('''SELECT * FROM trades WHERE ts LIKE ?
                            ORDER BY ts''', (f'{today}%',)).fetchall()
    conn.close()

    buys = {}
    wins = losses = 0
    total_pnl = 0.0
    pair_pnl  = {}

    for row in rows:
        sym = row['symbol']
        if row['action'] == 'BUY':
            buys[sym] = row['price']
        elif row['action'].startswith('SELL') and sym in buys:
            pnl = ((row['price'] - buys[sym]) / buys[sym]) * 100
            total_pnl += pnl
            pair_pnl[sym] = pair_pnl.get(sym, 0) + pnl
            wins   += 1 if pnl >= 0 else 0
            losses += 1 if pnl < 0  else 0
            del buys[sym]

    best  = max(pair_pnl, key=pair_pnl.get) if pair_pnl else '—'
    worst = min(pair_pnl, key=pair_pnl.get) if pair_pnl else '—'

    return {
        'date':          today,
        'total_trades':  wins + losses,
        'wins':          wins,
        'losses':        losses,
        'pnl_pct':       round(total_pnl, 2),
        'best_pair':     best,
        'worst_pair':    worst,
    }
