# ============================================================
#  MÓDULO: indicators.py
#  Cálculo de indicadores técnicos sobre buffer OHLCV
# ============================================================

import pandas as pd
import pandas_ta as ta
from config import (
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    EMA_FAST, EMA_SLOW,
    BB_PERIOD, BB_STD,
)


def compute_indicators(ohlcv: list[dict]) -> dict | None:
    """
    Recibe lista de velas OHLCV (al menos 30).
    Retorna dict con valores actuales de cada indicador,
    o None si no hay suficientes datos.
    """
    if len(ohlcv) < 30:
        return None

    df = pd.DataFrame(ohlcv, columns=["open", "high", "low", "close", "volume"])
    df = df.astype(float)

    # --- RSI --------------------------------------------------
    df.ta.rsi(length=RSI_PERIOD, append=True)
    rsi_col = f"RSI_{RSI_PERIOD}"

    # --- MACD -------------------------------------------------
    df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)
    macd_col   = f"MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"
    signal_col = f"MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"

    # --- EMA --------------------------------------------------
    df.ta.ema(length=EMA_FAST, append=True)
    df.ta.ema(length=EMA_SLOW, append=True)
    ema_fast_col = f"EMA_{EMA_FAST}"
    ema_slow_col = f"EMA_{EMA_SLOW}"

    # --- Bollinger Bands --------------------------------------
    df.ta.bbands(length=BB_PERIOD, std=BB_STD, append=True)
    bb_upper_col = f"BBU_{BB_PERIOD}_{BB_STD}_{BB_STD}"
    bb_lower_col = f"BBL_{BB_PERIOD}_{BB_STD}_{BB_STD}"

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    # Verificar que todos los indicadores tienen valores
    required = [rsi_col, macd_col, signal_col, ema_fast_col,
                ema_slow_col, bb_upper_col, bb_lower_col]
    if any(col not in df.columns for col in required):
        return None
    if df[required].iloc[-1].isna().any():
        return None

    return {
        "close":      float(last["close"]),
        "rsi":        float(last[rsi_col]),
        "macd":       float(last[macd_col]),
        "macd_sig":   float(last[signal_col]),
        "macd_prev":  float(prev[macd_col]),
        "sig_prev":   float(prev[signal_col]),
        "ema_fast":   float(last[ema_fast_col]),
        "ema_slow":   float(last[ema_slow_col]),
        "ema_fast_p": float(prev[ema_fast_col]),
        "ema_slow_p": float(prev[ema_slow_col]),
        "bb_upper":   float(last[bb_upper_col]),
        "bb_lower":   float(last[bb_lower_col]),
    }


def generate_signal(ind: dict) -> tuple[str, int, dict]:
    """
    Evalúa indicadores y retorna:
      - acción:        "BUY" | "SELL" | "HOLD"
      - confirmaciones: int (0-4)
      - detalle:       dict con estado de cada indicador

    Lógica:
      BUY  requiere MIN_CONFIRMATIONS de 4 señales alcistas
      SELL requiere MIN_CONFIRMATIONS de 4 señales bajistas
    """
    from config import MIN_CONFIRMATIONS

    buy_signals  = {}
    sell_signals = {}

    # 1. RSI
    buy_signals["rsi"]  = ind["rsi"] < RSI_OVERSOLD
    sell_signals["rsi"] = ind["rsi"] > RSI_OVERBOUGHT

    # 2. MACD crossover (señal en vela actual vs anterior)
    macd_cross_up   = (ind["macd_prev"] < ind["sig_prev"]) and (ind["macd"] >= ind["macd_sig"])
    macd_cross_down = (ind["macd_prev"] > ind["sig_prev"]) and (ind["macd"] <= ind["macd_sig"])
    buy_signals["macd"]  = macd_cross_up
    sell_signals["macd"] = macd_cross_down

    # 3. EMA golden/death cross
    golden_cross = (ind["ema_fast_p"] < ind["ema_slow_p"]) and (ind["ema_fast"] >= ind["ema_slow"])
    death_cross  = (ind["ema_fast_p"] > ind["ema_slow_p"]) and (ind["ema_fast"] <= ind["ema_slow"])
    buy_signals["ema"]  = golden_cross
    sell_signals["ema"] = death_cross

    # 4. Bollinger Bands (toca o cruza banda)
    buy_signals["bb"]  = ind["close"] <= ind["bb_lower"]
    sell_signals["bb"] = ind["close"] >= ind["bb_upper"]

    buy_count  = sum(buy_signals.values())
    sell_count = sum(sell_signals.values())

    detail = {
        "rsi":  f"{ind['rsi']:.1f}",
        "macd": "↑cross" if buy_signals["macd"] else ("↓cross" if sell_signals["macd"] else "flat"),
        "ema":  "golden" if buy_signals["ema"]  else ("death"  if sell_signals["ema"]  else "—"),
        "bb":   f"close={ind['close']:.4f} L={ind['bb_lower']:.4f} U={ind['bb_upper']:.4f}",
    }

    if buy_count >= MIN_CONFIRMATIONS:
        return "BUY", buy_count, detail
    elif sell_count >= MIN_CONFIRMATIONS:
        return "SELL", sell_count, detail
    else:
        return "HOLD", max(buy_count, sell_count), detail
