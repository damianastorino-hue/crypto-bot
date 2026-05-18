# ============================================================
#  MÓDULO: indicators.py  v2
#  Lógica de señal: Opción B — RSI obligatorio + 1 más
# ============================================================

import pandas as pd
import pandas_ta as ta
import numpy as np
from config import (
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    EMA_FAST, EMA_SLOW,
    BB_PERIOD, BB_STD,
    SIGNAL_MODE,
)


def compute_indicators(ohlcv: list[dict]) -> dict | None:
    """
    Recibe lista de velas OHLCV (al menos 30).
    Retorna dict con valores actuales, o None si faltan datos.
    """
    if len(ohlcv) < 30:
        return None

    df = pd.DataFrame(ohlcv, columns=["open", "high", "low", "close", "volume"])
    df = df.astype(float)

    df.ta.rsi(length=RSI_PERIOD, append=True)
    df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL, append=True)
    df.ta.ema(length=EMA_FAST, append=True)
    df.ta.ema(length=EMA_SLOW, append=True)
    df.ta.bbands(length=BB_PERIOD, std=BB_STD, append=True)

    rsi_col      = f"RSI_{RSI_PERIOD}"
    macd_col     = f"MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"
    signal_col   = f"MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"
    ema_fast_col = f"EMA_{EMA_FAST}"
    ema_slow_col = f"EMA_{EMA_SLOW}"
    bb_upper_col = f"BBU_{BB_PERIOD}_{BB_STD}_{BB_STD}"
    bb_lower_col = f"BBL_{BB_PERIOD}_{BB_STD}_{BB_STD}"

    required = [rsi_col, macd_col, signal_col, ema_fast_col,
                ema_slow_col, bb_upper_col, bb_lower_col]

    if any(col not in df.columns for col in required):
        return None
    if df[required].iloc[-1].isna().any():
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

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
    OPCIÓN B: RSI es condición OBLIGATORIA + mínimo 1 confirmación adicional.

    BUY:  RSI < RSI_OVERSOLD  AND  (MACD_cross_up OR golden_cross OR bb_touch_lower)
    SELL: RSI > RSI_OVERBOUGHT AND  (MACD_cross_down OR death_cross OR bb_touch_upper)

    Retorna: (acción, confirmaciones_adicionales, detalle)
    """

    # --- Calcular cada señal individual ---
    rsi_buy  = ind["rsi"] < RSI_OVERSOLD
    rsi_sell = ind["rsi"] > RSI_OVERBOUGHT

    macd_cross_up   = (ind["macd_prev"] < ind["sig_prev"]) and (ind["macd"] >= ind["macd_sig"])
    macd_cross_down = (ind["macd_prev"] > ind["sig_prev"]) and (ind["macd"] <= ind["macd_sig"])

    golden_cross = (ind["ema_fast_p"] < ind["ema_slow_p"]) and (ind["ema_fast"] >= ind["ema_slow"])
    death_cross  = (ind["ema_fast_p"] > ind["ema_slow_p"]) and (ind["ema_fast"] <= ind["ema_slow"])

    bb_buy  = ind["close"] <= ind["bb_lower"]
    bb_sell = ind["close"] >= ind["bb_upper"]

    # --- Confirmaciones adicionales (sin contar RSI) ---
    extra_buy  = sum([macd_cross_up,  golden_cross, bb_buy])
    extra_sell = sum([macd_cross_down, death_cross,  bb_sell])

    detail = {
        "rsi":  f"{ind['rsi']:.1f}",
        "macd": "↑" if macd_cross_up  else ("↓" if macd_cross_down else "·"),
        "ema":  "↑" if golden_cross   else ("↓" if death_cross     else "·"),
        "bb":   "↓" if bb_buy        else ("↑" if bb_sell          else "·"),
    }

    if SIGNAL_MODE == "rsi_plus_one":
        # RSI obligatorio + al menos 1 confirmación adicional
        if rsi_buy and extra_buy >= 1:
            return "BUY",  extra_buy, detail
        elif rsi_sell and extra_sell >= 1:
            return "SELL", extra_sell, detail
        else:
            # HOLD — pero informamos cuántas confirmaciones hay
            # para el semáforo del dashboard
            if rsi_buy:
                detail["estado"] = "RSI_OK_ESPERANDO"   # amarillo
            elif ind["rsi"] < 45:
                detail["estado"] = "ACERCANDOSE"         # amarillo tenue
            else:
                detail["estado"] = "NEUTRO"              # gris
            return "HOLD", extra_buy if rsi_buy else 0, detail

    else:
        # Modo legacy: cualquier 3 de 4
        buy_total  = int(rsi_buy)  + extra_buy
        sell_total = int(rsi_sell) + extra_sell
        if buy_total >= 3:
            return "BUY",  buy_total,  detail
        elif sell_total >= 3:
            return "SELL", sell_total, detail
        else:
            return "HOLD", max(buy_total, sell_total), detail
