"""Live Finnhub data feed for the Price Action Trading Terminal (US market).

Mirrors the Dhan ``LiveSignalEngine`` interface so the same Gradio UI
patterns can be reused for the US tab:

- resolves a US ticker (AAPL, TSLA, SPY, ...) to a Finnhub instrument
- seeds the strategy with recent intraday candles via Finnhub REST
- streams real-time trades/quotes via Finnhub WebSocket and builds
  timeframe candles
- recomputes price-action signals on every closed candle

Paper-trade only - no US broker is integrated.  The "order preview"
shows a paper trade instruction.
"""

from __future__ import annotations

import os
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import websocket

from strategy import PriceActionStrategy
from trend_reversal import TrendReversalStrategy
from breakout_breakdown import BreakoutBreakdownStrategy
from ai_signal import AISignalEngine

load_dotenv()

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")

TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60}


def resolve_symbol(symbol: str) -> dict[str, Any]:
    """Resolve a US ticker to Finnhub instrument details."""
    query = symbol.strip().upper()
    if not query:
        raise ValueError("symbol must not be empty")
    return {
        "symbol": query,
        "display": query,
        "instrument_type": "US_EQUITY",
    }


class FinnhubLiveEngine:
    """Streams Finnhub ticks, builds candles and emits price-action signals."""

    def __init__(
        self,
        symbol: str,
        timeframe: str = "5m",
        strategy: str = "price_action",
        fast_ema: int = 20,
        slow_ema: int = 50,
        pivot_left: int = 3,
        pivot_right: int = 3,
        seed_days: int = 7,
    ) -> None:
        if timeframe not in TIMEFRAME_MINUTES:
            raise ValueError(f"timeframe must be one of {list(TIMEFRAME_MINUTES)}")
        self.provider_name = "finnhub"
        self.seed_provider = "finnhub"
        self._validate_credentials()
        self._finnhub = self._make_feed_client()

        resolved = resolve_symbol(symbol)
        self.symbol = resolved["symbol"]
        self.display = resolved["display"]
        self.instrument_type = resolved["instrument_type"]

        self.timeframe = timeframe
        self.minutes = TIMEFRAME_MINUTES[timeframe]
        self.seed_days = int(seed_days)
        self.seed_source = "unknown"

        sl = strategy.lower()
        if sl in ("trend", "trend reversal", "trend_reversal"):
            self.strategy = TrendReversalStrategy()
        elif "breakout" in sl:
            self.strategy = BreakoutBreakdownStrategy()
        else:
            self.strategy = PriceActionStrategy(timeframe, fast_ema, slow_ema, pivot_left, pivot_right)

        self.history: pd.DataFrame | None = None
        self.current: dict[str, Any] | None = None
        self._feed = None
        self._thread: threading.Thread | None = None
        self._ws = None
        self.status = "idle"
        self.error = ""
        self.lock = threading.Lock()

        self._ws_ready = threading.Event()

        # AI surface - same shape as LiveSignalEngine.
        self.ai_engine = AISignalEngine()
        self._ai_llm_applied: tuple[str, str] = ("", "")
        self._ai_force = False
        self._ai_key: Any = None
        self._ai_value: dict[str, Any] | None = None
        self._ai_computing = False
        self._last_snapshot: dict[str, Any] | None = None
        self._last_closed: dict[str, Any] | None = None
        self._ai_history: list[dict[str, Any]] = []

        # US market has no integrated broker -> option surface is empty.
        self.option_segment = None
        self.option_underlying = None


    # ---------------------------------------------------------------- helpers

    def _validate_credentials(self) -> None:
        """Validate API credentials from the environment and store them."""
        api_key = (os.environ.get("FINNHUB_API_KEY") or "").strip()
        if not api_key:
            raise ValueError(
                "FINNHUB_API_KEY not set in .env. "
                "Get a free key at https://finnhub.io/register"
            )
        self._api_key = api_key

    def _make_feed_client(self):
        """Build the REST data client used for seeding history."""
        import finnhub
        return finnhub.Client(api_key=self._api_key)

    def _bucket_start(self, dt: datetime) -> datetime:
        minutes_of_day = dt.hour * 60 + dt.minute
        bucket = (minutes_of_day // self.minutes) * self.minutes
        return dt.replace(hour=bucket // 60, minute=bucket % 60, second=0, microsecond=0)

    def _rollover(self) -> None:
        if self.current is None:
            return
        c = self.current
        volume = max(0.0, c.get("volume_base", 0) - c.get("_vol_at_open", 0))
        if volume <= 0:
            volume = float(c["ticks"])
        row = pd.DataFrame(
            [
                {
                    "timestamp": c["timestamp"],
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                    "volume": volume,
                }
            ]
        )
        self.history = pd.concat([self.history, row], ignore_index=True)
        self.history = self.history.drop_duplicates(subset=["timestamp"], keep="last")
        self.history = self.history.sort_values("timestamp").reset_index(drop=True)

    # ---------------------------------------------------------------- start / stop

    def start(self) -> str:
        """Seed history, open the WebSocket and return the initial status."""
        try:
            self._seed()
        except Exception as exc:
            self.error = f"{self.provider_name} seed: {exc}"
            self.status = f"error: {exc}"
            return self.status

        try:
            self._open_stream()
        except Exception as exc:
            self.error = f"{self.provider_name} stream: {exc}"
            self.status = f"error: {exc}"
            return self.status
        return self.status

    def stop(self) -> None:
        """Close the WebSocket and reset state."""
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None
            self._feed = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.status = "stopped"

    # ---------------------------------------------------------------- seed

    def _finnhub_to_rows(self, response: dict) -> list[dict]:
        rows = []
        for timestamp, open_price, high, low, close, volume in zip(
            response.get("t", []),
            response.get("o", []),
            response.get("h", []),
            response.get("l", []),
            response.get("c", []),
            response.get("v", []),
        ):
            rows.append(
                {
                    "timestamp": datetime.fromtimestamp(int(timestamp), tz=UTC),
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume),
                }
            )
        return rows

    def _seed_finnhub(self) -> list[dict] | None:
        """Historical candles via Finnhub REST; None when the key is not allowed."""
        try:
            to = datetime.now(UTC).replace(second=0, microsecond=0)
            fro = to - timedelta(days=self.seed_days)
            response = self._finnhub.stock_candles(
                self.symbol,
                str(self.minutes),
                int(fro.timestamp()),
                int(to.timestamp()),
            )
        except Exception:
            return None
        if response.get("s") != "ok":
            return None
        return self._finnhub_to_rows(response)

    def _seed_yfinance(self) -> list[dict] | None:
        """Free, keyless fallback for historical candles."""
        try:
            import yfinance as yf
        except Exception:
            return None
        interval = {5: "5m", 15: "15m", 60: "1h"}[self.minutes]
        period = f"{max(int(self.seed_days), 1)}d"
        try:
            df = yf.download(
                self.symbol,
                interval=interval,
                period=period,
                progress=False,
                auto_adjust=True,
            )
        except Exception:
            return None
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)
        rows = []
        for ts, row in df.iterrows():
            if pd.isna(row.get("Close")):
                continue
            candle_time = ts.to_pydatetime()
            if candle_time.tzinfo is None or candle_time.utcoffset() is None:
                candle_time = candle_time.replace(tzinfo=UTC)
            candle_time = candle_time.astimezone(UTC)
            rows.append(
                {
                    "timestamp": candle_time,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                }
            )
        return rows

    def _seed(self) -> None:
        columns = ["timestamp", "open", "high", "low", "close", "volume"]
        self.history = pd.DataFrame(columns=columns)
        current_bucket = self._bucket_start(datetime.now(IST))

        rows = self._seed_finnhub()
        self.seed_source = self.seed_provider
        if not rows:
            rows = self._seed_yfinance()
            self.seed_source = "yfinance"
        if not rows:
            self.seed_source = "none"
            self.error = (
                f"History unavailable from {self.provider_name} and the yfinance "
                "fallback returned no data; continuing with live ticks."
            )
            return

        rows = [r for r in rows if r["timestamp"] < current_bucket]
        if not rows:
            return
        self.history = pd.DataFrame(rows, columns=columns)
        self.history = self.history.drop_duplicates(
            subset=["timestamp"], keep="last"
        ).sort_values("timestamp").reset_index(drop=True)

    # ---------------------------------------------------------------- stream

    def _open_stream(self) -> None:
        # finnhub-python is REST-only (no .websocket()).  Connect directly
        # to Finnhub's WebSocket endpoint via websocket-client.
        url = f"wss://ws.finnhub.io?token={self._api_key}"
        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self._feed = self._ws
        self._thread = threading.Thread(
            target=self._ws_run_forever, args=(self._ws,), daemon=True
        )
        self._thread.start()

        # Wait for on_open to fire (subscribe happens there).
        deadline = time.time() + 8.0
        while time.time() < deadline and not self._ws_ready.is_set():
            time.sleep(0.1)
        if not self._ws_ready.is_set():
            self._close_ws()
            self._ws = None
            self._feed = None
            self._thread = None
            raise RuntimeError("Finnhub WebSocket did not connect within 8s")

        self.status = (
            f"LIVE {self.display} (US/EQUITY) {self.timeframe} | "
            "stream connected, waiting for ticks"
        )

    def _ws_run_forever(self, ws: websocket.WebSocketApp) -> None:
        try:
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            pass

    def _close_ws(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            pass

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        try:
            ws.send(json.dumps({"type": "subscribe", "symbol": self.symbol}))
        except Exception as exc:
            self.error = f"finnhub ws subscribe: {exc}"
        self._ws_ready.set()

    def _on_ws_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        """Finnhub WebSocket callback."""
        try:
            msg = json.loads(message)
        except (ValueError, TypeError):
            return
        msg_type = msg.get("type")
        if msg_type in ("trade", "quote"):
            self._on_tick(msg)
        elif msg_type == "errorMessage":
            self.error = f"finnhub WS: {msg.get('message') or msg}"

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        self.error = f"finnhub WS error: {error}"

    def _on_ws_close(self, ws: websocket.WebSocketApp, code: int, msg: str) -> None:
        pass

    def _on_tick(self, msg: dict) -> None:
        """Update the in-progress candle from a Finnhub trade/quote tick."""
        if msg.get("type") == "trade":
            data = msg.get("data")
            if isinstance(data, list) and data:
                tick = data[-1]
                try:
                    price = float(tick.get("p", tick.get("price", 0)))
                except (TypeError, ValueError):
                    return
                try:
                    ts = datetime.fromtimestamp(int(tick.get("t", 0)), tz=UTC).astimezone(IST)
                except (TypeError, ValueError):
                    ts = datetime.now(IST)
                volume = float(tick.get("v", tick.get("volume", 0)))
            else:
                return
        else:  # quote
            try:
                price = float(msg.get("p", msg.get("cp", msg.get("close", 0))))
            except (TypeError, ValueError):
                return
            ts = datetime.now(IST)
            volume = 0.0

        if price <= 0:
            return

        now = ts
        bucket_start = self._bucket_start(now)
        with self.lock:
            if self.current is None or bucket_start > self.current["start"]:
                self._rollover()
                self.current = {
                    "start": bucket_start,
                    "timestamp": bucket_start + timedelta(minutes=self.minutes),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume_base": volume,
                    "_vol_at_open": volume,
                    "ticks": 1,
                }
            else:
                c = self.current
                c["high"] = max(c["high"], price)
                c["low"] = min(c["low"], price)
                c["close"] = price
                c["ticks"] += 1
                c["volume_base"] = c.get("volume_base", 0) + volume

    # ---------------------------------------------------------------- read

    def snapshot(self) -> tuple[dict[str, Any] | None, pd.DataFrame | None]:
        """Return the latest signal summary and the display DataFrame.

        Signals are always evaluated on *completed* candles only. The
        in-progress live candle is appended for display but never drives a
        signal.
        """
        with self.lock:
            if self.history is None or self.history.empty:
                return None, None

            closed = self.strategy.generate_signal(self.history.copy())
            latest_closed = closed.iloc[-1]

            display = closed
            live_price: float
            if self.current is not None:
                cur = self.current
                volume = max(0.0, cur.get("volume_base", 0) - cur.get("_vol_at_open", 0))
                if volume <= 0:
                    volume = float(cur["ticks"])
                current_row = pd.DataFrame(
                    [
                        {
                            "timestamp": cur["timestamp"],
                            "open": cur["open"],
                            "high": cur["high"],
                            "low": cur["low"],
                            "close": cur["close"],
                            "volume": volume,
                        }
                    ]
                )
                display = pd.concat([display, current_row], ignore_index=True)
                live_price = cur["close"]
            else:
                live_price = float(latest_closed["close"])

            summary: dict[str, Any] = {
                "display": self.display,
                "timeframe": self.timeframe,
                "live_price": live_price,
                "candle_time": (
                    str(self.current["timestamp"]) if self.current else str(latest_closed["timestamp"])
                ),
                "closes_at": (
                    str(self.current["timestamp"]) if self.current else "n/a"
                ),
                "trend": str(latest_closed["trend"]),
                "position": str(latest_closed["position"]),
                "support": float(latest_closed["support"]),
                "resistance": float(latest_closed["resistance"]),
                "ema_fast": float(latest_closed["ema_fast"]),
                "ema_slow": float(latest_closed["ema_slow"]),
                "signal": str(latest_closed["signal"]),
                "reason": str(latest_closed["reason"]),
                "seed_days": self.seed_days,
            }
            self._last_snapshot = summary
            self._last_closed = {
                "timestamp": str(latest_closed["timestamp"]),
                "signal": str(latest_closed["signal"]),
                "price": float(latest_closed["close"]),
                "reason": str(latest_closed["reason"]),
                "position": str(latest_closed["position"]),
            }
            return summary, display


    # ---------------------------------------------------------------- AI

    def set_llm(self, provider: str | None = None, model: str | None = None) -> None:
        """Select an LLM provider/model from the UI; forces the next AI pass."""
        sig = ((provider or "").strip(), (model or "").strip())
        if sig != self._ai_llm_applied:
            self.ai_engine.set_llm(provider=provider, model=model)
            self._ai_llm_applied = sig
            self._ai_force = True

    def ai_signal(self, force: bool = False) -> dict[str, Any]:
        """AI verdict on the latest closed candle (computed once per candle).

        Heavy model fitting is skipped while an earlier analysis is still
        running; callers poll again on the next tick for the cached result.
        ``force=True`` recomputes now.
        """
        with self.lock:
            if self.history is None:
                return {"status": "idle", "note": "no data yet"}
            latest = self.history["timestamp"].iloc[-1]
            frame = self.history.copy()

        force = force or self._ai_force
        self._ai_force = False
        if not force and self._ai_key == latest:
            return self._ai_value or {"status": "computing"}

        if self._ai_computing:
            return {"status": "computing", "note": "AI analysis in progress"}

        self._ai_computing = True
        try:
            res = self.ai_engine.analyze(frame)
            res["status"] = "ok"
        except Exception as exc:
            res = {"status": "error", "note": str(exc)}
        finally:
            self._ai_computing = False

        self._ai_key = latest
        self._ai_value = res
        if res.get("status") == "ok":
            self._ai_history.append(
                {
                    "timestamp": res["timestamp"],
                    "signal": res["signal"],
                    "confidence": round(float(res["confidence"]), 2),
                    "score": round(float(res["score"]), 2),
                }
            )
            self._ai_history = self._ai_history[-200:]
        res["history"] = list(self._ai_history)
        return res

    # ---------------------------------------------------------------- orders (paper)

    def trade_preview(self, quantity: int = 5, sl_pct: float = 2.0) -> str:
        """Build a human-readable paper trade preview for the current signal."""
        with self.lock:
            lc = self._last_closed
            if lc is None or lc["signal"] not in ("BUY", "SELL"):
                return (
                    f"{self.display} - no paper trade trigger yet "
                    f"(latest closed candle is {lc['signal'] if lc else 'HOLD'})."
                )

            price = lc["price"]
            side = "BUY" if lc["signal"] == "BUY" else "SELL"
            stops = round(
                price * (1 + sl_pct / 100.0) if side == "SELL"
                else price * (1 - sl_pct / 100.0),
                2,
            )

        report = [
            "--- PAPER TRADE (US market - no live broker) ---",
            f"Symbol:     {self.display}",
            f"Signal:     {lc['signal']} @ {price:.2f}",
            f"Timeframe:  {self.timeframe}",
            f"Action:     {side} {quantity} share(s)",
            f"Entry:      LIMIT @ {price:.2f}",
            f"Stop-loss:  {'LIMIT' if side == 'SELL' else 'SLM'} @ {stops:.2f}",
            f"Exit:       opposite signal or stop",
            "NOTE:        Paper only - connect a US broker to execute for real.",
            "---------------------",
        ]
        return "\n".join(report)

    def place_signal_order(self, quantity: int = 5, sl_pct: float = 2.0) -> str:
        """Paper-trade order - same shape as Dhan's place_signal_order."""
        return self.trade_preview(quantity, sl_pct)

    def place_option_order(self, security_id: str, lots: int = 1, sl_pct: float = 2.0) -> str:
        """US options are not integrated in this feed."""
        return "US options are not available through this Finnhub feed yet."
