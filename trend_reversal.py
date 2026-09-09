"""Trend Reversal strategy (multi-timeframe confluence).

Bias comes from a higher timeframe, structure from an intermediate one and
the entry signal from the base (finest) candle series:

- 1h   : 200-period EMA slope defines the macro bias (uptrend/downtrend).
- 15m  : swing high/low zones (support/resistance) plus RSI swing
         divergence and/or a touch of the zone build the setup.
- base : candle patterns (engulfing / pin bar) + 9/21 EMA cross + RSI
         confirmation produce the entry trigger.

Output schema matches PriceActionStrategy so the same UI tables work:
`trend` = 1h bias, `support`/`resistance` = 15m zones, `ema_fast`/`ema_slow`
= base 9/21 EMA, plus `signal`/`reason`/`position`.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(50.0)


def _pivots(values: pd.Series, kind: str, k: int = 2) -> pd.Series:
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    for i in range(k, len(arr) - k):
        win = arr[i - k : i + k + 1]
        if kind == "low" and arr[i] == win.min() and (win == arr[i]).sum() == 1:
            out[i] = arr[i]
        elif kind == "high" and arr[i] == win.max() and (win == arr[i]).sum() == 1:
            out[i] = arr[i]
    return pd.Series(out, index=values.index)


def _divergence(value: pd.Series, rsi: pd.Series, kind: str) -> pd.Series:
    """RSI divergence using the last two swing points so far (no look-ahead)."""
    n = len(value)
    piv = value.to_numpy(dtype=float)
    r = rsi.to_numpy(dtype=float)
    out = np.zeros(n, dtype=bool)
    for i in range(n):
        idx = [j for j in range(i) if not np.isnan(piv[j])]
        if len(idx) < 2:
            continue
        j1, j0 = idx[-2], idx[-1]
        if kind == "bull":
            out[i] = (piv[j0] < piv[j1]) and (r[j0] > r[j1])
        else:
            out[i] = (piv[j0] > piv[j1]) and (r[j0] < r[j1])
    return pd.Series(out, index=value.index)


class TrendReversalStrategy:
    """Multi-timeframe trend-reversal signal generator."""

    name = "Trend Reversal"

    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        htf_ema: int = 200,
        rsi_period: int = 14,
        swing_k: int = 2,
        rsi_trigger_buy: float = 40.0,
        rsi_trigger_sell: float = 60.0,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.htf_ema = htf_ema
        self.rsi_period = rsi_period
        self.swing_k = swing_k
        self.rsi_trigger_buy = rsi_trigger_buy
        self.rsi_trigger_sell = rsi_trigger_sell

    def generate_signal(self, df: pd.DataFrame) -> pd.DataFrame:
        base = df.copy()
        base["timestamp"] = pd.to_datetime(base["timestamp"])
        base = base.sort_values("timestamp").reset_index(drop=True)

        # ---- higher timeframe (1h): 200 EMA slope bias ------------------
        h1 = base.set_index("timestamp").resample("1h", label="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna().reset_index()
        h1["ema_htf"] = h1["close"].ewm(span=self.htf_ema, adjust=False).mean()
        slope = h1["ema_htf"].diff()
        h1["bias"] = np.where(
            slope > 0, "UPTREND", np.where(slope < 0, "DOWNTREND", "SIDEWAYS")
        )

        # ---- intermediate (15m): zones + RSI divergence -----------------
        m15 = base.set_index("timestamp").resample("15min", label="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna().reset_index()
        m15["rsi15"] = _rsi(m15["close"], self.rsi_period)
        piv_low = _pivots(m15["low"], "low", self.swing_k)
        piv_high = _pivots(m15["high"], "high", self.swing_k)
        m15["support"] = piv_low.shift(self.swing_k).ffill()
        m15["resistance"] = piv_high.shift(self.swing_k).ffill()
        m15["div_bull"] = _divergence(m15["low"].shift(self.swing_k), m15["rsi15"].shift(self.swing_k), "bull")
        m15["div_bear"] = _divergence(m15["high"].shift(self.swing_k), m15["rsi15"].shift(self.swing_k), "bear")

        # ---- base: 9/21 EMA, RSI, candle patterns -----------------------
        b = base
        b["ema_fast"] = b["close"].ewm(span=self.ema_fast, adjust=False).mean()
        b["ema_slow"] = b["close"].ewm(span=self.ema_slow, adjust=False).mean()
        b["rsi"] = _rsi(b["close"], self.rsi_period)
        b["cross_up"] = (b["ema_fast"].shift(1) <= b["ema_slow"].shift(1)) & (b["ema_fast"] > b["ema_slow"])
        b["cross_down"] = (b["ema_fast"].shift(1) >= b["ema_slow"].shift(1)) & (b["ema_fast"] < b["ema_slow"])

        body = (b["close"] - b["open"]).abs()
        upper = b["high"] - np.maximum(b["open"], b["close"])
        lower = np.minimum(b["open"], b["close"]) - b["low"]
        b["pin_bull"] = (lower >= 2 * body) & (lower >= 2 * upper) & (body > 0)
        b["pin_bear"] = (upper >= 2 * body) & (upper >= 2 * lower) & (body > 0)
        prev_bear = (b["close"].shift(1) < b["open"].shift(1))
        prev_bull = (b["close"].shift(1) > b["open"].shift(1))
        bear_body = (b["open"].shift(1) - b["close"].shift(1)).abs()
        bull_body = (b["close"].shift(1) - b["open"].shift(1)).abs()
        b["engulf_bull"] = prev_bear & ~prev_bear.isna() & (
            (b["close"] > b["open"])
            & (body >= bear_body)
            & (b["close"] > b["open"].shift(1))
            & (b["open"] < b["close"].shift(1))
        )
        b["engulf_bear"] = prev_bull & ~prev_bull.isna() & (
            (b["close"] < b["open"])
            & (body >= bull_body)
            & (b["close"] < b["open"].shift(1))
            & (b["open"] > b["close"].shift(1))
        )

        # ---- merge multi-timeframe context onto base --------------------
        b["_m15"] = b["timestamp"].dt.floor("15min")
        b["_h1"] = b["timestamp"].dt.floor("1h")
        tr15 = m15[["timestamp", "support", "resistance", "div_bull", "div_bear"]].rename(
            columns={"timestamp": "_m15"}
        )
        tr1 = h1[["timestamp", "bias"]].rename(columns={"timestamp": "_h1"})
        b = b.merge(tr15, on="_m15", how="left")
        b = b.merge(tr1, on="_h1", how="left")
        b["trend"] = b["bias"].ffill().fillna("SIDEWAYS")

        # ---- confluence rules -------------------------------------------
        b["at_support"] = b["support"].notna() & (b["low"] <= b["support"] * 1.005)
        b["at_resistance"] = b["resistance"].notna() & (b["high"] >= b["resistance"] * 0.995)

        bull_bias = b["trend"] == "UPTREND"
        mid_bull = b["at_support"] | b["div_bull"]
        entry_bull = ((b["pin_bull"] | b["engulf_bull"])
                      & b["cross_up"] & (b["rsi"] > self.rsi_trigger_buy))

        bear_bias = b["trend"] == "DOWNTREND"
        mid_bear = b["at_resistance"] | b["div_bear"]
        entry_bear = ((b["pin_bear"] | b["engulf_bear"])
                      & b["cross_down"] & (b["rsi"] < self.rsi_trigger_sell))

        b["signal"] = "HOLD"
        b["reason"] = ""
        b.loc[bull_bias & mid_bull & entry_bull, "signal"] = "BUY"
        b.loc[bull_bias & mid_bull & entry_bull, "reason"] = (
            "1h UPTREND + 15m support/divergence + 5m engulf/pin & 9/21 cross & RSI"
        )
        b.loc[bear_bias & mid_bear & entry_bear, "signal"] = "SELL"
        b.loc[bear_bias & mid_bear & entry_bear, "reason"] = (
            "1h DOWNTREND + 15m resistance/divergence + 5m engulf/pin & 9/21 cross & RSI"
        )

        pos = "FLAT"
        b["position"] = "FLAT"
        for i in range(1, len(b)):
            if b.at[i, "signal"] == "BUY":
                pos = "LONG"
            elif b.at[i, "signal"] == "SELL":
                pos = "SHORT"
            b.at[i, "position"] = pos

        b = b.drop(columns=["_m15", "_h1"], errors="ignore")
        return b

    def latest_signal(self, df: pd.DataFrame) -> dict[str, Any]:
        r = self.generate_signal(df).iloc[-1]
        f = lambda x: float(x) if pd.notna(x) else float("nan")  # noqa: E731
        return {
            "timeframe": "5m/15m/1h",
            "price": f(r["close"]),
            "trend": f"{r['trend']}",
            "support": f(r["support"]),
            "resistance": f(r["resistance"]),
            "signal": str(r["signal"]),
            "reason": str(r["reason"]),
            "position": str(r["position"]),
            "ema_fast": f(r["ema_fast"]),
            "ema_slow": f(r["ema_slow"]),
        }