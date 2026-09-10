"""Backend manager: owns the live Alpaca engine, watchlist pricing, and the
payload builders behind the Next.js terminal UI.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from alpaca_feed import AlpacaLiveEngine
from finnhub_feed import FinnhubLiveEngine
from server import indicators

load_dotenv()

UTC = ZoneInfo("UTC")

DEFAULT_WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "SPY", "QQQ"]
DEFAULT_TIMEFRAME = "5m"

US_PRESET_SYMBOLS = [
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT",
    "AMZN", "META", "GOOGL", "AMD", "JPM",
]

DHAN_PRESET_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK",
    "BHARTIARTL", "ITC", "LT", "AXISBANK", "BAJFINANCE", "HINDUNILVR",
    "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIENT", "MARUTI", "SUNPHARMA", "TITAN",
    "NIFTY 50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX",
]

_PREV_CLOSE_TTL = 60.0
_SNAPSHOT_CACHE_MS = 1500  # debounce expensive indicator recompute


def _has_alpaca_keys() -> bool:
    return bool((os.environ.get("APCA_API_KEY_ID") or "").strip()) and bool(
        (os.environ.get("APCA_API_SECRET_KEY") or "").strip()
    )


def _has_finnhub_keys() -> bool:
    return bool((os.environ.get("FINNHUB_API_KEY") or "").strip())


def _has_dhan_keys() -> bool:
    return bool((os.environ.get("DHAN_CLIENT_ID") or "").strip()) and bool(
        (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
    )


def _has_us_keys() -> bool:
    return _has_alpaca_keys() or _has_finnhub_keys()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


class TerminalManager:
    """Holds one active live-trading engine (US Alpaca/Finnhub or Dhan NSE)
    plus watchlist quote plumbing."""

    def __init__(self, watchlist: list[str] | None = None) -> None:
        self.symbols = list(watchlist or DEFAULT_WATCHLIST)
        self.custom_symbols: dict[str, list[str]] = {"us": [], "dhan": []}
        self.active_symbol: str = ""
        self.market: str = "us"  # "us" | "dhan"
        self.provider: str = self._default_us_provider()
        self.engine: AlpacaLiveEngine | FinnhubLiveEngine | LiveSignalEngine | None = None
        self._hist: StockHistoricalDataClient | None = None
        self._prev_close_cache: tuple[float, dict] | None = None
        self._clock_cache: tuple[float, dict] | None = None
        self._last_snapshot: dict | None = None
        self._last_snapshot_at: float = 0.0
        self._selection: tuple = ()
        self._lock = threading.Lock()
        self._pending_llm: tuple[str, str, str] = (
            (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower(),
            (os.environ.get("LLM_MODEL") or "").strip(),
            "",
        )

    # ------------------------------------------------------------------ config

    @staticmethod
    def _default_us_provider() -> str:
        return "finnhub" if (_has_finnhub_keys() and not _has_alpaca_keys()) else "alpaca"

    @property
    def configured(self) -> bool:
        return _has_us_keys()

    @property
    def markets(self) -> dict:
        return {
            "us": {"configured": _has_us_keys(), "alpaca": _has_alpaca_keys(), "finnhub": _has_finnhub_keys()},
            "dhan": {"configured": _has_dhan_keys()},
        }

    def _hist_client(self) -> StockHistoricalDataClient | None:
        if not self.configured:
            return None
        if self._hist is None:
            self._hist = StockHistoricalDataClient(
                api_key=os.environ["APCA_API_KEY_ID"],
                secret_key=os.environ["APCA_API_SECRET_KEY"],
            )
        return self._hist

    # ------------------------------------------------------------------ engine

    def select(
        self,
        symbol: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        strategy: str = "price_action",
        provider: str = "",
        fast_ema: int = 20,
        slow_ema: int = 50,
        pivot_left: int = 3,
        pivot_right: int = 3,
    ) -> dict:
        symbol = (symbol or "").strip().upper()
        provider = (provider or "").strip().lower() or self._default_us_provider()
        if provider == "alpaca" and os.environ.get("DISABLE_ALPACA_STREAM", "").strip() == "1":
            provider = "finnhub"
        if not symbol:
            raise ValueError("symbol is required")
        if provider not in ("alpaca", "finnhub"):
            raise ValueError("provider must be 'alpaca' or 'finnhub'")
        if provider == "alpaca" and not _has_alpaca_keys():
            raise RuntimeError(
                "Alpaca keys not configured. Set APCA_API_KEY_ID / "
                "APCA_API_SECRET_KEY in .env"
            )
        if provider == "finnhub" and not _has_finnhub_keys():
            raise RuntimeError(
                "Finnhub key not configured. Set FINNHUB_API_KEY in .env"
            )

        cls = AlpacaLiveEngine if provider == "alpaca" else FinnhubLiveEngine
        with self._lock:
            selection = ("us", provider, symbol, timeframe, strategy, fast_ema, slow_ema, pivot_left, pivot_right)
            if (
                self.engine is not None
                and self.market == "us"
                and self._selection == selection
                and self.engine.status not in ("stopped", "idle")
            ):
                return self._engine_result("ok")

            self.stop_unlocked()
            engine = cls(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                fast_ema=int(fast_ema),
                slow_ema=int(slow_ema),
                pivot_left=int(pivot_left),
                pivot_right=int(pivot_right),
            )
            self._apply_pending_llm(engine)
            self.engine = engine
            self.market = "us"
            self.provider = provider
            self.active_symbol = symbol
            self._selection = selection
            status = engine.start()
            if (
                provider == "alpaca"
                and status.lower().startswith("error:")
                and _has_finnhub_keys()
            ):
                try:
                    engine.stop()
                except Exception:
                    pass
                fallback = FinnhubLiveEngine(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy=strategy,
                    fast_ema=int(fast_ema),
                    slow_ema=int(slow_ema),
                    pivot_left=int(pivot_left),
                    pivot_right=int(pivot_right),
                )
                self._apply_pending_llm(fallback)
                self.engine = fallback
                self.provider = "finnhub"
                status = fallback.start()
                if not status.lower().startswith("error:"):
                    status = f"{status} (Alpaca connection limit; using Finnhub fallback)"
            return self._engine_result(status)

    def select_dhan(
        self,
        symbol: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        strategy: str = "price_action",
        fast_ema: int = 20,
        slow_ema: int = 50,
        pivot_left: int = 3,
        pivot_right: int = 3,
    ) -> dict:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if not _has_dhan_keys():
            raise RuntimeError(
                "Dhan keys not configured. Set DHAN_CLIENT_ID / "
                "DHAN_ACCESS_TOKEN in .env"
            )

        from dhan_feed import LiveSignalEngine  # lazy: heavy deps

        with self._lock:
            selection = ("dhan", symbol, timeframe, strategy, fast_ema, slow_ema, pivot_left, pivot_right)
            if (
                self.engine is not None
                and self.market == "dhan"
                and self._selection == selection
                and self.engine.status not in ("stopped", "idle")
            ):
                return self._engine_result("ok")

            self.stop_unlocked()
            engine = LiveSignalEngine(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy,
                fast_ema=int(fast_ema),
                slow_ema=int(slow_ema),
                pivot_left=int(pivot_left),
                pivot_right=int(pivot_right),
            )
            self._apply_pending_llm(engine)
            self.engine = engine
            self.market = "dhan"
            self.provider = "dhan"
            self.active_symbol = symbol
            self._selection = selection
            status = engine.start()
            return self._engine_result(status)

    def _engine_result(self, status: str) -> dict:
        return {
            "symbol": self.active_symbol,
            "market": self.market,
            "provider": self.provider,
            "status": status,
            "engine": self._engine_brief(),
        }

    def _engine_brief(self) -> dict | None:
        if self.engine is None:
            return None
        return {
            "symbol": self.engine.symbol,
            "market": self.market,
            "provider": self.provider,
            "timeframe": self.engine.timeframe,
            "status": self.engine.status,
            "seed_source": getattr(self.engine, "seed_source", self.provider),
        }

    def preset_symbols(self, market: str = "us") -> dict:
        market = (market or "").strip().lower()
        if market == "dhan":
            choices = list(DHAN_PRESET_SYMBOLS)
        else:
            choices = list(US_PRESET_SYMBOLS)
        return {"market": "dhan" if market == "dhan" else "us", "symbols": choices}

    def stop(self) -> None:
        with self._lock:
            self.stop_unlocked()

    def stop_unlocked(self) -> None:
        if self.engine is not None:
            try:
                self.engine.stop()
            except Exception:
                pass
            self.engine = None
        self.active_symbol = ""
        self.market = "us"
        self.provider = self._default_us_provider()
        self._selection = ()
        self._last_snapshot = None

    # ------------------------------------------------------------------ watchlist

    def watchlist(self, market: str = "us") -> dict:
        market = "dhan" if (market or "").strip().lower() == "dhan" else "us"
        base_symbols = DEFAULT_WATCHLIST if market == "us" else DHAN_PRESET_SYMBOLS[:6]
        symbols = list(dict.fromkeys(base_symbols + self.custom_symbols[market]))
        rows = []
        error = ""
        client = self._hist_client() if market == "us" else None
        trades: dict = {}
        price_by_symbol: dict = {}
        prev_by_symbol: dict = {}
        if client is not None:
            try:
                trades = client.get_stock_latest_trade(
                    StockLatestTradeRequest(
                        symbol_or_symbols=symbols, feed=DataFeed.IEX
                    )
                ) or {}
                for sym, trade in trades.items():
                    p = getattr(trade, "price", None)
                    if p is not None:
                        price_by_symbol[sym] = float(p)
            except Exception as exc:
                error = f"watchlist quotes: {exc}"
        elif market == "dhan":
            price_by_symbol, prev_by_symbol, error = self._dhan_watchlist_quotes(symbols)

        prev_close = self._prev_closes() if market == "us" else prev_by_symbol

        for sym in symbols:
            price = price_by_symbol.get(sym)
            prev = prev_close.get(sym)
            change = change_pct = None
            if price is not None and prev:
                change_pct = (price / prev - 1.0) * 100.0
                change = price - prev
            rows.append(
                {
                    "symbol": sym,
                    "price": round(price, 2) if price is not None else None,
                    "change": round(change, 2) if change is not None else None,
                    "changePct": round(change_pct, 2) if change_pct is not None else None,
                    "active": sym == self.active_symbol,
                }
            )
        return {
            "rows": rows,
            "error": error or None,
            "configured": self.markets[market]["configured"],
            "market": market,
        }

    def _dhan_watchlist_quotes(self, symbols: list[str]) -> tuple[dict, dict, str]:
        try:
            from dhan_feed import resolve_symbol
            from dhanhq import DhanContext, dhanhq

            client_id = (os.environ.get("DHAN_CLIENT_ID") or "").strip()
            access_token = (os.environ.get("DHAN_ACCESS_TOKEN") or "").strip()
            if not client_id or not access_token:
                return {}, {}, "Dhan credentials are not configured"

            dhan = dhanhq(DhanContext(client_id, access_token))
            grouped: dict[str, list[int]] = {}
            symbol_by_key: dict[tuple[str, str], str] = {}
            for symbol in symbols:
                info = resolve_symbol(symbol)
                segment = str(info["api_segment"])
                security_id = str(info["security_id"])
                grouped.setdefault(segment, []).append(int(security_id))
                symbol_by_key[(segment, security_id)] = symbol

            prices: dict[str, float] = {}
            previous: dict[str, float] = {}
            for segment, ids in grouped.items():
                payload = (dhan.ticker_data({segment: ids}).get("data") or {}).get("data") or {}
                for sid, quote in (payload.get(segment) or {}).items():
                    symbol = symbol_by_key.get((segment, str(sid)))
                    if not symbol or not isinstance(quote, dict):
                        continue
                    try:
                        prices[symbol] = float(quote["last_price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    ohlc = quote.get("ohlc") or {}
                    try:
                        previous[symbol] = float(ohlc["close"])
                    except (KeyError, TypeError, ValueError):
                        pass
            return prices, previous, ""
        except Exception as exc:  # noqa: BLE001 - keep watchlist refresh alive
            return {}, {}, f"Dhan watchlist quotes: {exc}"

    def add_to_watchlist(self, market: str, symbol: str) -> dict:
        market = "dhan" if (market or "").strip().lower() == "dhan" else "us"
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if len(symbol) > 30:
            raise ValueError("symbol is too long")
        base_symbols = DEFAULT_WATCHLIST if market == "us" else DHAN_PRESET_SYMBOLS
        if symbol not in base_symbols and symbol not in self.custom_symbols[market]:
            self.custom_symbols[market].append(symbol)
        return self.watchlist(market)

    def remove_from_watchlist(self, market: str, symbol: str) -> dict:
        market = "dhan" if (market or "").strip().lower() == "dhan" else "us"
        symbol = (symbol or "").strip().upper()
        self.custom_symbols[market] = [s for s in self.custom_symbols[market] if s != symbol]
        return self.watchlist(market)

    def _prev_closes(self) -> dict:
        client = self._hist_client()
        if client is None:
            return {}
        now = datetime.now(UTC).timestamp()
        if self._prev_close_cache is not None and now - self._prev_close_cache[0] < _PREV_CLOSE_TTL:
            return self._prev_close_cache[1]
        result: dict = {}
        try:
            from alpaca.data.requests import StockBarsRequest

            resp = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=self.symbols,
                    timeframe=TimeFrame(1, cast(TimeFrameUnit, TimeFrameUnit.Day)),
                    start=datetime.now(UTC) - timedelta(days=6),
                    end=datetime.now(UTC) + timedelta(days=1),
                    feed=DataFeed.IEX,
                    adjustment=Adjustment.RAW,
                )
            )
            for sym in self.symbols:
                sym_bars = getattr(resp, "data", {}).get(sym) or []
                result[sym] = self._prev_close_for(sym_bars)
        except Exception:
            pass
        self._prev_close_cache = (now, result)
        return result

    @staticmethod
    def _prev_close_for(sym_bars: list) -> float | None:
        """Last fully completed daily bar close.

        When the market is already open today, Alpaca returns a partial bar
        for the current session - step back one more bar in that case.
        """
        today = datetime.now(UTC).date()
        if not sym_bars:
            return None
        last = sym_bars[-1]
        if last.timestamp.date() < today:
            return float(last.close)
        if len(sym_bars) >= 2:
            return float(sym_bars[-2].close)
        return None

    # ------------------------------------------------------------------ snapshots

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        """Canonical form used for symbol matching.

        Strips spaces and, when the Dhan feed module is available, maps index
        naming variants ("NIFTYIFT", "BANK NIFTY", "NIFTY50", ...) to one
        canonical family so alias inputs match the active engine.
        """
        q = (symbol or "").strip().upper().replace(" ", "")
        try:
            from dhan_feed import _normalize_index_query

            q = _normalize_index_query(q).replace(" ", "")
        except Exception:
            pass
        return q

    def engine_for(self, symbol: str | None) -> AlpacaLiveEngine | FinnhubLiveEngine | LiveSignalEngine:
        """Raise a clear error when no engine matches `symbol`."""
        requested = (symbol or "").strip().upper()
        if self.engine is None:
            raise RuntimeError("no symbol selected - call /api/select first")
        if requested and self._canonical_symbol(requested) != self._canonical_symbol(
            self.engine.symbol
        ):
            raise RuntimeError(
                f"engine for {self.engine.symbol} is active; request symbol={requested} "
                "or stop the engine first"
            )
        return self.engine

    def snapshot_payload(self, symbol: str | None = None) -> dict:
        engine = self.engine_for(symbol)
        if engine.history is None or engine.history.empty:
            return {
                "status": engine.status,
                "error": engine.error or "no candles yet",
                "seed_source": getattr(engine, "seed_source", self.provider),
                "market": self.market,
                "provider": self.provider,
            }

        try:
            summary, _ = engine.snapshot()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

        if summary is None:
            return {"status": engine.status, "error": engine.error or "no signal"}

        # Indicator panel (closed candles only), debounced.
        ind = self._indicator_panel()
        ai = self.last_ai()

        side = str(summary["position"] or "FLAT").upper()
        if side == "FLAT":
            side = "WATCH" if summary["signal"] == "HOLD" else "FLAT"
        confidence = ai.get("confidence") if ai and ai.get("confidence") else indicators.confidence(ind)

        return {
            "status": engine.status,
            "symbol": engine.symbol,
            "display": summary.get("display") or engine.symbol,
            "timeframe": engine.timeframe,
            "seed_source": getattr(engine, "seed_source", self.provider),
            "market": self.market,
            "provider": self.provider,
            "price": summary["live_price"],
            "candle_time": summary["candle_time"],
            "closes_at": summary["closes_at"],
            "signal": {
                "side": side,
                "strategy": summary["signal"],
                "trend": summary.get("trend", ""),
                "reason": summary.get("reason") or "",
                "confidence": confidence,
                "source": "ai" if ai and ai.get("confidence") else "heuristic",
            },
            "indicators": {
                "ema9": ind["ema9"],
                "ema21": ind["ema21"],
                "ema169": ind["ema169"],
                "vwap": ind["vwap"],
                "rsi": ind["rsi"],
                "rvol": ind["rvol"],
                "support": ind["support"],
                "resistance": ind["resistance"],
                "bos": ind["bos"],
                "fvg": ind["fvg"],
            },
            "ai": ai,
        }

    def _indicator_panel(self, force: bool = False) -> dict:
        now = datetime.now(UTC).timestamp() * 1000.0
        if not force and self._last_snapshot is not None and now - self._last_snapshot_at < _SNAPSHOT_CACHE_MS:
            return self._last_snapshot

        engine = self.engine_for(None)
        if engine.history is None or engine.history.empty:
            raise RuntimeError("no candles available for indicators")
        df = engine.history.copy()
        cols = indicators.indicator_columns(df)
        bosr = indicators.bos(df)
        fvgr = indicators.fvg(df)

        ind = {
            "ema9": indicators._f(cols["ema9"].iloc[-1]),
            "ema21": indicators._f(cols["ema21"].iloc[-1]),
            "ema169": indicators._f(cols["ema169"].iloc[-1]),
            "vwap": indicators._f(cols["vwap"].iloc[-1]),
            "rsi": indicators._f(cols["rsi"].iloc[-1]),
            "rvol": indicators._f(cols["rvol"].iloc[-1]),
            "support": indicators._f(engine.history["low"].tail(50).min()),
            "resistance": indicators._f(engine.history["high"].tail(50).max()),
            "bos": bosr["state"],
            "bos_side": bosr["side"],
        }
        ind["fvg"] = {"found": fvgr["found"], "side": fvgr.get("side")}
        ind["fvg_state"] = fvgr["side"] if fvgr["found"] else None
        self._last_snapshot = ind
        self._last_snapshot_at = now
        return ind

    # ------------------------------------------------------------------ candles

    def candles(self, symbol: str | None, limit: int = 160) -> dict:
        engine = self.engine_for(symbol)
        if engine.history is None or engine.history.empty:
            return {
                "symbol": engine.symbol,
                "bars": [],
                "seed_source": getattr(engine, "seed_source", self.provider),
            }

        # Compute signal/trend for each bar using the strategy
        df = indicators.indicator_columns(engine.history.copy()).tail(limit).copy()
        try:
            sig_df = engine.strategy.generate_signal(engine.history.copy()).tail(limit)
            df["signal"] = sig_df["signal"].values if "signal" in sig_df.columns else None
            df["trend"] = sig_df["trend"].values if "trend" in sig_df.columns else None
            df["position"] = sig_df["position"].values if "position" in sig_df.columns else None
        except Exception:
            pass

        bars = []
        for _, r in df.iterrows():
            bars.append(
                {
                    "time": _iso(r["timestamp"]),
                    "t": int(r["timestamp"].timestamp()),
                    "open": indicators._f(r["open"]),
                    "high": indicators._f(r["high"]),
                    "low": indicators._f(r["low"]),
                    "close": indicators._f(r["close"]),
                    "volume": indicators._f(r["volume"]),
                    "ema9": indicators._f(r["ema9"]),
                    "ema21": indicators._f(r["ema21"]),
                    "ema169": indicators._f(r["ema169"]),
                    "vwap": indicators._f(r["vwap"]),
                    "rsi": indicators._f(r["rsi"]),
                    "signal": r.get("signal") if r.get("signal") is not None else None,
                    "trend": r.get("trend") if r.get("trend") is not None else None,
                    "position": r.get("position") if r.get("position") is not None else None,
                }
            )
        return {"symbol": engine.symbol, "timeframe": engine.timeframe, "seed_source": getattr(engine, "seed_source", self.provider), "bars": bars}

    # ------------------------------------------------------------------ AI

    def _apply_pending_llm(
        self, engine: AlpacaLiveEngine | FinnhubLiveEngine | LiveSignalEngine
    ) -> None:
        provider, model, api_key = self._pending_llm
        if not provider:
            return
        setter = getattr(engine, "set_llm", None)
        if callable(setter):
            try:
                setter(provider=provider, model=model, api_key=api_key)
            except Exception:
                pass

    def set_llm(self, provider: str = "", model: str = "", api_key: str = "") -> dict:
        """Apply an LLM provider/model (like the Gradio UI).

        Applies immediately when an engine is active; otherwise stores the
        selection and applies it the next time an engine is created.
        """
        provider = (provider or "").strip().lower()
        model = (model or "").strip()
        api_key = (api_key or "").strip()
        with self._lock:
            self._pending_llm = (provider, model, api_key)
            engine = self.engine
        if engine is not None:
            setter = getattr(engine, "set_llm", None)
            if callable(setter):
                try:
                    setter(provider=provider, model=model, api_key=api_key)
                except Exception:
                    pass
        return {
            "provider": provider or None,
            "model": model or None,
            "configured": True,
        }

    def last_ai(self) -> dict | None:
        engine = self.engine_for(None)
        try:
            res = engine.ai_signal(force=False)
        except Exception:
            return None
        if res.get("status") != "ok":
            return None
        return {
            "timestamp": res.get("timestamp"),
            "signal": res.get("signal"),
            "confidence": float(res["confidence"]) if res.get("confidence") else None,
            "score": float(res["score"]) if res.get("score") is not None else None,
            "reason": res.get("reason", ""),
        }

    def ai_payload(self, symbol: str | None = None, refresh: bool = False) -> dict:
        engine = self.engine_for(symbol)
        try:
            res = engine.ai_signal(force=refresh)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        if res.get("status") != "ok":
            return {"status": res.get("status"), "note": res.get("note", ""), "error": res.get("error", "")}
        return {
            "status": "ok",
            "symbol": engine.symbol,
            "timestamp": res.get("timestamp"),
            "analyzed_at": res.get("analyzed_at"),
            "price": float(res["price"]) if res.get("price") is not None else None,
            "signal": res.get("signal"),
            "confidence": float(res["confidence"]) if res.get("confidence") else None,
            "score": float(res["score"]) if res.get("score") is not None else None,
            "reason": res.get("reason", ""),
            "indicators": res.get("indicators", {}),
            "models": res.get("models", {}),
            "strategies": res.get("strategies", {}),
            "llm": res.get("llm"),
            "llm_signal": res.get("llm_signal"),
            "llm_enabled": bool(res.get("llm_enabled")),
            "history": res.get("history", []),
        }

    def order_preview(self, symbol: str | None, quantity: int, sl_pct: float) -> dict:
        engine = self.engine_for(symbol)
        return {
            "symbol": engine.symbol,
            "quantity": int(quantity),
            "sl_pct": float(sl_pct),
            "preview": engine.trade_preview(int(quantity), float(sl_pct)),
            "paper_only": True,
        }

    # ------------------------------------------------------------------ websocket

    def ws_payload(self) -> dict:
        if self.engine is None:
            return {"type": "status", "status": "idle", "message": "select a symbol first"}
        engine = self.engine
        if engine.status.startswith(("idle", "stopped")):
            return {"type": "status", "status": engine.status, "error": engine.error}
        try:
            summary, _ = engine.snapshot()
        except Exception as exc:
            return {"type": "error", "message": str(exc)}
        if summary is None:
            return {"type": "status", "status": engine.status, "error": engine.error,
                "seed_source": getattr(engine, "seed_source", self.provider)}

        live = engine.current
        live_candle = None
        if live is not None:
            volume = max(0.0, live.get("volume_base", 0) - live.get("_vol_at_open", 0)) or float(live.get("ticks", 0))
            live_candle = {
                "open": live["open"],
                "high": live["high"],
                "low": live["low"],
                "close": live["close"],
                "volume": max(volume, 0.0),
                "start": _iso(live["start"]),
                "closes_at": _iso(live["timestamp"]),
            }
        return {
            "type": "update",
            "symbol": engine.symbol,
            "market": self.market,
            "provider": self.provider,
            "seed_source": getattr(engine, "seed_source", self.provider),
            "timeframe": engine.timeframe,
            "price": summary["live_price"],
            "candle_time": summary["candle_time"],
            "closes_at": summary["closes_at"],
            "live_candle": live_candle,
            "signal": {
                "side": str(summary["position"]).upper(),
                "strategy": summary["signal"],
                "trend": summary.get("trend", ""),
                "reason": summary.get("reason") or "",
            },
        }

    # ------------------------------------------------------------------ health

    def health(self) -> dict:
        return {
            "status": "ok",
            "configured": self.configured,
            "markets": self.markets,
            "market": self.market,
            "provider": self.provider,
            "market_open": indicators.market_open(),
            "active_symbol": self.active_symbol,
            "engine": self._engine_brief(),
        }


manager = TerminalManager()


def factory() -> TerminalManager:
    return manager