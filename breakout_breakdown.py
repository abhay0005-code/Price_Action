"""Breakout / Breakdown strategy (multi-timeframe market structure).

Market-structure hierarchy with entry triggers on the base candle series:

- 1h   (macro)  : 1h EMA sets the macro trend and confirmed 1h pivot
                  highs/lows are the major support/resistance zones.
- 15m  (middle) : 15m swing high/low ranges confirm structural
                  breakouts/breakdowns; RSI swing divergence at a 1h
                  level flags exhaustion (reversal setup).
- base (5m)     : 9/21 EMA cross + volume spike + close beyond the 5m
                  swing high/low is the precise entry trigger.

Signals
-------
BUY  (breakout)  : 1h/15m close above a key resistance (1h level or 15m
                   high) AND 5m fast EMA crosses above slow EMA AND
                   volume >= mult * avg AND close above the 5m swing high.
SELL (breakdown) : mirrored below 1h support / 15m low.
BUY  (reversal)  : after a breakdown, price touches 1h support in a
                   DOWNTREND, 15m RSI shows a bullish divergence
                   (oversold lower-low / higher-RSI-low) and 5m closes
                   back above its slow EMA and the 5m swing high.
SELL (reversal)  : mirrored at 1h resistance in an UPTREND with a bearish
                   divergence and a 5m close back below EMA/swing low.

Output schema matches PriceActionStrategy / TrendReversalStrategy so the
same UI tables work.  `trend` = 1h bias, `support`/`resistance` = 1h
zones, `ema_fast`/`ema_slow` = base 9/21 EMA, plus `signal`/`reason`/
`position`.
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


def _divergence(
    value: pd.Series, rsi: pd.Series, kind: str, rsi_bound: float = 0.0
) -> pd.Series:
    """RSI divergence using the last two swing points so far (no look-ahead).

    ``kind="bull"``: price makes a lower low while RSI makes a higher low
    and the RSI at the first (oversold) swing low is below ``rsi_bound``.
    ``kind="bear"`` mirrors it above the (overbought) ``rsi_bound``.
    """
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
            out[i] = (
                (piv[j0] < piv[j1])
                and (r[j0] > r[j1])
                and (r[j1] < rsi_bound)
            )
        else:
            out[i] = (
                (piv[j0] > piv[j1])
                and (r[j0] < r[j1])
                and (r[j1] > rsi_bound)
            )
    return pd.Series(out, index=value.index)


class BreakoutBreakdownStrategy:
    """Multi-timeframe breakout / breakdown / reversal signal generator."""

    name = "Breakout / Breakdown"

    def __init__(
        self,
        ema_fast: int = 9,
        ema_slow: int = 21,
        htf_ema: int = 50,
        rsi_period: int = 14,
        swing_k: int = 2,
        m15_lookback: int = 6,
        m5_lookback: int = 4,
        volume_mult: float = 1.5,
        volume_ma: int = 20,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        align_with_htf: bool = True,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.htf_ema = htf_ema
        self.rsi_period = rsi_period
        self.swing_k = swing_k
        self.m15_lookback = m15_lookback
        self.m5_lookback = m5_lookback
        self.volume_mult = volume_mult
        self.volume_ma = volume_ma
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.align_with_htf = align_with_htf

    def generate_signal(self, df: pd.DataFrame) -> pd.DataFrame:
        base = df.copy()
        base["timestamp"] = pd.to_datetime(base["timestamp"])
        base = base.sort_values("timestamp").reset_index(drop=True)

        # ---- higher timeframe (1h): macro trend + key levels -----------
        h1 = base.set_index("timestamp").resample("1h", label="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna().reset_index()
        h1["ema_htf"] = h1["close"].ewm(span=self.htf_ema, adjust=False).mean()
        h1["trend"] = np.where(
            h1["close"] > h1["ema_htf"], "UPTREND",
            np.where(h1["close"] < h1["ema_htf"], "DOWNTREND", "SIDEWAYS"),
        )
        piv_high = _pivots(h1["high"], "high", self.swing_k)
        piv_low = _pivots(h1["low"], "low", self.swing_k)
        h1["resistance"] = piv_high.shift(self.swing_k).ffill()
        h1["support"] = piv_low.shift(self.swing_k).ffill()

        # ---- intermediate (15m): swing range + RSI divergence ----------
        m15 = base.set_index("timestamp").resample("15min", label="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna().reset_index()
        m15["rsi15"] = _rsi(m15["close"], self.rsi_period)
        m15["resistance"] = (
            m15["high"].rolling(self.m15_lookback).max().shift(1)
        )
        m15["support"] = m15["low"].rolling(self.m15_lookback).min().shift(1)
        m15["div_bull"] = _divergence(
            m15["low"].shift(self.swing_k), m15["rsi15"].shift(self.swing_k),
            "bull", self.rsi_oversold,
        )
        m15["div_bear"] = _divergence(
            m15["high"].shift(self.swing_k), m15["rsi15"].shift(self.swing_k),
            "bear", self.rsi_overbought,
        )

        # ---- base series: EMAs, cross, volume, swing range -------------
        b = base
        b["ema_fast"] = b["close"].ewm(span=self.ema_fast, adjust=False).mean()
        b["ema_slow"] = b["close"].ewm(span=self.ema_slow, adjust=False).mean()
        b["cross_up"] = (
            (b["ema_fast"].shift(1) <= b["ema_slow"].shift(1))
            & (b["ema_fast"] > b["ema_slow"])
        )
        b["cross_down"] = (
            (b["ema_fast"].shift(1) >= b["ema_slow"].shift(1))
            & (b["ema_fast"] < b["ema_slow"])
        )
        vol_avg = b["volume"].rolling(self.volume_ma).mean().shift(1)
        b["high_vol"] = (vol_avg > 0) & (b["volume"] >= self.volume_mult * vol_avg)
        b["swing_high5"] = b["high"].rolling(self.m5_lookback).max().shift(1)
        b["swing_low5"] = b["low"].rolling(self.m5_lookback).min().shift(1)
        b["above_ema"] = b["close"] > b["ema_slow"]
        b["below_ema"] = b["close"] < b["ema_slow"]
        b["above_swing"] = b["close"] > b["swing_high5"]
        b["below_swing"] = b["close"] < b["swing_low5"]

        # ---- merge multi-timeframe context onto base -------------------
        b["_m15"] = b["timestamp"].dt.floor("15min")
        b["_h1"] = b["timestamp"].dt.floor("1h")
        tr15 = m15[
            ["timestamp", "support", "resistance", "div_bull", "div_bear"]
        ].rename(columns={"timestamp": "_m15", "support": "m15_support",
                          "resistance": "m15_resistance"})
        tr1 = h1[["timestamp", "trend", "support", "resistance"]].rename(
            columns={"timestamp": "_h1", "trend": "h1_trend",
                     "support": "h1_support", "resistance": "h1_resistance"}
        )
        b = b.merge(tr15, on="_m15", how="left")
        b = b.merge(tr1, on="_h1", how="left")
        b["trend"] = b["h1_trend"].ffill().fillna("SIDEWAYS")
        b["support"] = b["h1_support"]
        b["resistance"] = b["h1_resistance"]

        # ---- signal rules ----------------------------------------------
        b["at_h1_support"] = (
            b["h1_support"].notna() & (b["low"] <= b["h1_support"] * 1.005)
        )
        b["at_h1_resistance"] = (
            b["h1_resistance"].notna() & (b["high"] >= b["h1_resistance"] * 0.995)
        )

        break_buy = (
            (b["close"] > b["h1_resistance"]) | (b["close"] > b["m15_resistance"])
        )
        break_sell = (
            (b["close"] < b["h1_support"]) | (b["close"] < b["m15_support"])
        )
        trigger_buy = b["cross_up"] & b["high_vol"] & b["above_swing"]
        trigger_sell = b["cross_down"] & b["high_vol"] & b["below_swing"]

        trend_ok_buy = b["trend"] != "DOWNTREND" if self.align_with_htf else True
        trend_ok_sell = b["trend"] != "UPTREND" if self.align_with_htf else True

        reclaim_bull = b["above_ema"] & b["above_swing"]
        reclaim_bear = b["below_ema"] & b["below_swing"]

        bull_rev = (
            (b["trend"] == "DOWNTREND")
            & b["at_h1_support"]
            & b["div_bull"]
            & reclaim_bull
        )
        bear_rev = (
            (b["trend"] == "UPTREND")
            & b["at_h1_resistance"]
            & b["div_bear"]
            & reclaim_bear
        )

        b["signal"] = "HOLD"
        b["reason"] = ""
        b.loc[break_buy & trigger_buy & trend_ok_buy, "signal"] = "BUY"
        b.loc[break_buy & trigger_buy & trend_ok_buy, "reason"] = (
            "1h/15m BREAKOUT (close > key resistance / 15m high) + "
            "5m 9/21 EMA cross + volume spike + close > 5m swing high"
        )
        b.loc[break_sell & trigger_sell & trend_ok_sell, "signal"] = "SELL"
        b.loc[break_sell & trigger_sell & trend_ok_sell, "reason"] = (
            "1h/15m BREAKDOWN (close < key support / 15m low) + "
            "5m 9/21 EMA cross + volume spike + close < 5m swing low"
        )
        b.loc[bull_rev, "signal"] = "BUY"
        b.loc[bull_rev, "reason"] = (
            "REVERSAL 1h DOWNTREND hits support + 15m RSI bull divergence "
            "(oversold) + 5m reclaims EMA/swing resistance"
        )
        b.loc[bear_rev, "signal"] = "SELL"
        b.loc[bear_rev, "reason"] = (
            "REVERSAL 1h UPTREND hits resistance + 15m RSI bear divergence "
            "(overbought) + 5m loses EMA/swing support"
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
            "timeframe": "5m base / 15m / 1h",
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