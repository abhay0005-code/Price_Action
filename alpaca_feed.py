"""Live Alpaca data feed for the Price Action Trading Terminal (US market).

Mirrors the ``FinnhubLiveEngine`` interface so the same Gradio UI patterns can
be reused for the US tab:

- resolves a US ticker (AAPL, TSLA, SPY, ...) to an Alpaca instrument
- seeds the strategy with recent intraday candles via Alpaca REST
- streams real-time trades/quotes via Alpaca's Market Data WebSocket and
  reuses the parent engine's candle builder / signal logic
- recomputes price-action signals on every closed candle

Paper-trade only - no US broker is integrated.  The "order preview" shows a
paper trade instruction.

Credentials are read from ``.env``:
- ``APCA_API_KEY_ID``  - Alpaca API key (paper or live)
- ``APCA_API_SECRET_KEY`` - Alpaca API secret
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from finnhub_feed import FinnhubLiveEngine, IST, UTC


class AlpacaLiveEngine(FinnhubLiveEngine):
    """Streams Alpaca IData ticks, builds candles and emits signals."""

    provider_name = "alpaca"
    seed_provider = "alpaca"

    def _validate_credentials(self) -> None:
        api_key = (os.environ.get("APCA_API_KEY_ID") or "").strip()
        secret_key = (os.environ.get("APCA_API_SECRET_KEY") or "").strip()
        if not api_key or not secret_key:
            raise ValueError(
                "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set in .env. "
                "Create a free Alpaca account (paper works) at "
                "https://alpaca.markets and generate API keys "
                "in https://app.alpaca.markets/paper"
            )
        self._api_key = api_key
        self._secret_key = secret_key

    def _make_feed_client(self):
        self._hist = StockHistoricalDataClient(self._api_key, self._secret_key)
        self._stream = StockDataStream(
            self._api_key,
            self._secret_key,
            feed=DataFeed.IEX,
        )
        return None

    # ---------------------------------------------------------------- seed

    def _seed_finnhub(self) -> list[dict] | None:
        """Historical candles via Alpaca REST (IEX feed)."""
        try:
            to = self._latest_close_time()
            if to is None:
                return None
            fro = to - timedelta(days=self.seed_days)
            bars = self._hist.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=self.symbol,
                    timeframe=(
                        TimeFrame(1, TimeFrameUnit.Hour)
                        if self.minutes == 60
                        else TimeFrame(self.minutes, TimeFrameUnit.Minute)
                    ),
                    start=fro,
                    end=to,
                    feed=DataFeed.IEX,
                    adjustment=Adjustment.RAW,
                )
            )
        except Exception:
            return None
        symbol_bars = bars.data.get(self.symbol) or []
        if not symbol_bars:
            return None

        rows = []
        for bar in symbol_bars:
            candle_time = bar.timestamp
            if isinstance(candle_time, str):
                raise ValueError("unexpected string timestamp from Alpaca")
            if candle_time.tzinfo is None or candle_time.utcoffset() is None:
                candle_time = candle_time.replace(tzinfo=UTC)
            candle_time = candle_time.astimezone(UTC)
            rows.append(
                {
                    "timestamp": candle_time,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
            )
        return rows

    def _latest_close_time(self) -> datetime | None:
        """UTC end time for the seed window (latest trade if the market is open)."""
        try:
            latest = self._hist.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=self.symbol, feed=DataFeed.IEX)
            )
        except Exception:
            latest = None
        if latest is not None:
            try:
                trade = latest[self.symbol]
            except Exception:
                trade = None
            if trade is not None:
                ts = getattr(trade, "timestamp", None)
                if ts is not None:
                    if ts.tzinfo is None or ts.utcoffset() is None:
                        ts = ts.replace(tzinfo=UTC)
                    return ts.astimezone(UTC).replace(second=0, microsecond=0)
        return datetime.now(UTC).replace(second=0, microsecond=0)

    # ---------------------------------------------------------------- stream

    def _open_stream(self) -> None:
        self._stream.subscribe_trades(self._on_alpaca_trade, self.symbol)
        self._stream.subscribe_quotes(self._on_alpaca_quote, self.symbol)
        self._feed = self._stream
        self._ws = self._stream
        self._thread = threading.Thread(
            target=self._ws_run_forever, args=(self._stream,), daemon=True
        )
        self._thread.start()

        deadline = time.time() + 8.0
        while time.time() < deadline and not self._stream_running():
            time.sleep(0.1)
        if not self._stream_running():
            self.stop()
            raise RuntimeError("Alpaca data stream did not connect within 8s")

        self.status = (
            f"LIVE {self.display} (US/EQUITY via Alpaca IEX) {self.timeframe} | "
            "stream connected, waiting for ticks"
        )

    def _stream_running(self) -> bool:
        ws = getattr(self, "_ws", None)
        return bool(
            ws is not None
            and getattr(ws, "_loop", None) is not None
            and getattr(ws, "_running", False)
        )

    def _ws_run_forever(self, ws: Any) -> None:
        try:
            ws.run()
        except Exception:
            pass

    async def _on_alpaca_trade(self, trade: Any) -> None:
        try:
            price = float(trade.price)
            size = float(trade.size)
            ts = trade.timestamp
        except (AttributeError, TypeError, ValueError):
            return
        if price <= 0:
            return
        if ts is not None:
            if ts.tzinfo is None or ts.utcoffset() is None:
                ts = ts.replace(tzinfo=UTC)
            ts = ts.astimezone(UTC)
        tick = {
            "type": "trade",
            "data": [
                {
                    "p": price,
                    "t": int(ts.timestamp()) if ts is not None else 0,
                    "v": size,
                }
            ],
        }
        self._on_tick(tick)

    async def _on_alpaca_quote(self, quote: Any) -> None:
        try:
            mid = (float(quote.bid_price) + float(quote.ask_price)) / 2.0
        except (AttributeError, TypeError, ValueError):
            return
        if mid <= 0:
            return
        self._on_tick({"type": "quote", "p": mid, "cp": mid, "close": mid})

    def stop(self) -> None:
        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                loop = getattr(stream, "_loop", None)
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.status = "stopped"