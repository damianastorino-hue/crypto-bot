# ============================================================
#  SCALPING BOT — indicators.py  v3
#  RSI obligatorio + 1 confirmación adicional (Opción B)
# ============================================================

import pandas as pd
import pandas_ta as ta  # noqa: F401  (se usa vía df.ta accessor)
from config import (
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    EMA_FAST, EMA_SLOW,
    BB_PERIOD, BB_STD,
    SIGNAL_MODE,
)


def compute_indicators(ohlcv: list[dict]) -> dict | None:
    """
    Recibe lista de velas OHLCV (mínimo 30).
    Retorna dict con valores del último cierre, o None si faltan datos.
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

    rsi_col    = f"RSI_{RSI_PERIOD}"
    macd_col   = f"MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"
    sig_col    = f"MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"
    ema_f_col  = f"EMA_{EMA_FAST}"
    ema_s_col  = f"EMA_{EMA_SLOW}"
    # pandas_ta genera BBU/BBL con el std como float, p.ej. BBU_20_2.0
    bb_u_col   = f"BBU_{BB_PERIOD}_{BB_STD}"
    bb_l_col   = f"BBL_{BB_PERIOD}_{BB_STD}"

    required = [rsi_col, macd_col, sig_col, ema_f_col, ema_s_col, bb_u_col, bb_l_col]

    # Verificar columnas y valores no-NaN
    if any(c not in df.columns for c in required):
        return None
    if df[required].iloc[-1].isna().any():
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    return {
        "close":      float(last["close"]),
        "rsi":        float(last[rsi_col]),
        "macd":       float(last[macd_col]),
        "macd_sig":   float(last[sig_col]),
        "macd_prev":  float(prev[macd_col]),
        "sig_prev":   float(prev[sig_col]),
        "ema_fast":   float(last[ema_f_col]),
        "ema_slow":   float(last[ema_s_col]),
        "ema_fast_p": float(prev[ema_f_col]),
        "ema_slow_p": float(prev[ema_s_col]),
        "bb_upper":   float(last[bb_u_col]),
        "bb_lower":   float(last[bb_l_col]),
    }


def generate_signal(ind: dict) -> tuple[str, int, dict]:
    """
    Opción B: RSI es condición OBLIGATORIA + mínimo 1 confirmación adicional.

    BUY:  RSI < RSI_OVERSOLD  AND  (MACD_cross_up OR golden_cross OR bb_lower_touch)
    SELL: RSI > RSI_OVERBOUGHT AND  (MACD_cross_down OR death_cross OR bb_upper_touch)

    Retorna: (acción, confirmaciones_extra, detalle)
    """
    rsi_buy  = ind["rsi"] < RSI_OVERSOLD
    rsi_sell = ind["rsi"] > RSI_OVERBOUGHT

    macd_up   = (ind["macd_prev"] < ind["sig_prev"]) and (ind["macd"] >= ind["macd_sig"])
    macd_down = (ind["macd_prev"] > ind["sig_prev"]) and (ind["macd"] <= ind["macd_sig"])

    golden = (ind["ema_fast_p"] < ind["ema_slow_p"]) and (ind["ema_fast"] >= ind["ema_slow"])
    death  = (ind["ema_fast_p"] > ind["ema_slow_p"]) and (ind["ema_fast"] <= ind["ema_slow"])

    bb_buy  = ind["close"] <= ind["bb_lower"]
    bb_sell = ind["close"] >= ind["bb_upper"]

    extra_buy  = int(macd_up)  + int(golden) + int(bb_buy)
    extra_sell = int(macd_down)+ int(death)  + int(bb_sell)

    detail = {
        "rsi":  f"{ind['rsi']:.1f}",
        "macd": "↑" if macd_up   else ("↓" if macd_down else "·"),
        "ema":  "↑" if golden    else ("↓" if death     else "·"),
        "bb":   "↓" if bb_buy   else ("↑" if bb_sell    else "·"),
    }

    if SIGNAL_MODE == "rsi_plus_one":
        if rsi_buy and extra_buy >= 1:
            return "BUY",  extra_buy, detail
        if rsi_sell and extra_sell >= 1:
            return "SELL", extra_sell, detail

        # HOLD — estado para semáforo
        if rsi_buy:
            detail["estado"] = "RSI_OK_ESPERANDO"
        elif ind["rsi"] < 45:
            detail["estado"] = "ACERCANDOSE"
        else:
            detail["estado"] = "NEUTRO"
        return "HOLD", extra_buy if rsi_buy else 0, detail

    # Modo legacy: cualquier 3 de 4
    buy_total  = int(rsi_buy)  + extra_buy
    sell_total = int(rsi_sell) + extra_sell
    if buy_total >= 3:
        return "BUY",  buy_total,  detail
    if sell_total >= 3:
        return "SELL", sell_total, detail
    detail["estado"] = "NEUTRO"
    return "HOLD", max(buy_total, sell_total), detail
