"""Indicator computations for the trading terminal API.

Pure pandas helpers built on top of closed candlesticks. Used by ``server.engine``
to produce the indicator panel values (RSI, RVOL, VWAP, EMA9/21/169, BOS, FVG)
and a heuristic confidence score used until an AI verdict is available.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
SESSION_START = dtime(9, 30)
SESSION_END = dtime(16, 0)


def _ny_session_flags(ts: pd.Series) -> pd.Series:
    local = pd.DatetimeIndex(ts).tz_convert(NY)
    minutes = local.hour * 60 + local.minute
    return (minutes >= 570) & (minutes < 960)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def rvol(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Relative volume: latest bar volume vs prior `lookback` average."""
    base = volume.rolling(lookback, min_periods=5).mean().shift(1)
    return (volume / base.replace(0.0, np.nan)).fillna(1.0)


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def vwap(df: pd.DataFrame, fallback: int = 78) -> pd.Series:
    """Session VWAP (cumulative from the NY 09:30 session open).

    Bars outside the session are filled with a rolling average so early morning
    rows do not blow up the chart.
    """
    d = df.copy()
    if d.empty:
        return pd.Series(name="vwap", dtype=float)
    in_session = _ny_session_flags(d["timestamp"])
    tp = (d["high"] + d["low"] + d["close"]) / 3.0
    local = pd.DatetimeIndex(d["timestamp"]).tz_convert(NY)
    key = pd.Series(local.date.astype(str), index=d.index).where(in_session)

    pv = (tp * d["volume"]).where(in_session)
    cv = d["volume"].where(in_session)
    cum_pv = pv.groupby(key).cumsum()
    cum_cv = cv.groupby(key).cumsum()
    out = cum_pv / cum_cv.replace(0.0, np.nan)
    out = out.where(in_session)
    return out.fillna(out.rolling(fallback, min_periods=2).mean())


def bos(df: pd.DataFrame, left: int = 3, right: int = 3) -> dict:
    """Break-of-structure vs the most recent confirmed swing high/low."""
    n = len(df)
    highs = np.full(n, np.nan)
    lows = np.full(n, np.nan)
    for i in range(left, n - right):
        wh = df["high"].iloc[i - left : i + right + 1]
        wl = df["low"].iloc[i - left : i + right + 1]
        if df["high"].iloc[i] == wh.max() and int((wh == df["high"].iloc[i]).sum()) == 1:
            highs[i] = df["high"].iloc[i]
        if df["low"].iloc[i] == wl.min() and int((wl == df["low"].iloc[i]).sum()) == 1:
            lows[i] = df["low"].iloc[i]
    confirmed_high = pd.Series(highs).shift(right)
    confirmed_low = pd.Series(lows).shift(right)

    prior_high = confirmed_high.iloc[:-1].ffill().iloc[-1] if n > 1 else np.nan
    prior_low = confirmed_low.iloc[:-1].ffill().iloc[-1] if n > 1 else np.nan
    close = df["close"].iloc[-1]
    up = (not pd.isna(prior_high)) and close > prior_high
    down = (not pd.isna(prior_low)) and close < prior_low
    return {
        "up": bool(up),
        "down": bool(down),
        "state": "YES" if (up or down) else "NO",
        "side": ("UP" if up else "DOWN") if (up or down) else None,
        "swing_high": _f(prior_high),
        "swing_low": _f(prior_low),
    }


def fvg(df: pd.DataFrame, recent: int = 10) -> dict:
    """Most recent fair-value gap (3-candle imbalance) near the current price."""
    n = len(df)
    if n < 5:
        return {"found": False}
    lo, hi, close = (df["low"].values, df["high"].values, df["close"].values)
    start = max(2, n - recent)
    found_side = None
    gap_top = gap_bottom = None
    for i in range(start, n):
        if lo[i] > hi[i - 2]:
            found_side = "bull"
            gap_bottom, gap_top = hi[i - 2], lo[i]
        elif hi[i] < lo[i - 2]:
            found_side = "bear"
            gap_top, gap_bottom = lo[i - 2], hi[i]
    if found_side is None:
        return {"found": False}
    dist_pct = (abs(close[-1] - gap_bottom) / gap_bottom) * 100.0 if gap_bottom else None
    return {
        "found": True,
        "side": found_side,
        "top": _f(gap_top),
        "bottom": _f(gap_bottom),
        "distance_pct": _f(dist_pct),
    }


def confidence(ind: dict) -> float:
    """Heuristic confidence when no AI verdict exists (0..1)."""
    c = 0.50
    if ind.get("bos") == "YES":
        c += 0.15
    if ind.get("fvg") is True:
        c += 0.08
    rsi_val = ind.get("rsi")
    if rsi_val is not None:
        c += min(0.15, abs(rsi_val - 50.0) / 50.0 * 0.15)
    rvol_val = ind.get("rvol")
    if rvol_val is not None and rvol_val > 1.0:
        c += min(0.12, (rvol_val - 1.0) * 0.04)
    return round(float(np.clip(c, 0.15, 0.95)), 4)


def indicator_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Attach ema9/21/169, vwap, rsi, rvol columns (closed candles)."""
    out = df.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema21"] = ema(out["close"], 21)
    out["ema169"] = ema(out["close"], 169)
    out["vwap"] = vwap(out)
    out["rsi"] = rsi(out["close"])
    out["rvol"] = rvol(out["volume"])
    return out


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def market_open(now: datetime | None = None) -> dict:
    """US equity market open flag based on NY trading hours.

    Production note: uses exchange hours on weekdays; no holiday calendar. A
    real deployment could replace this with the broker's market clock.
    """
    now = now or datetime.now(NY)
    et = now.astimezone(NY)
    weekday = et.weekday() < 5
    minutes = et.hour * 60 + et.minute
    open_now = weekday and SESSION_START <= et.time() <= SESSION_END
    return {
        "open": bool(open_now),
        "as_of": et.isoformat(),
        "next_open": et.replace(hour=9, minute=30, second=0, microsecond=0).isoformat()
        if not open_now
        else None,
    }