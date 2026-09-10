"""Live Dhan data feed for the Price Action Trading Terminal.

Ties the off-line PriceActionStrategy to a live DhanHQ WebSocket:

- resolves a symbol to a Dhan security_id / exchange segment
- seeds the strategy with recent intraday candles via the REST API
- streams ``MarketFeed`` Quote ticks and builds timeframe candles
- recomputes price-action signals on every closed candle
"""

from __future__ import annotations

import os
import threading
import time
import warnings
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from dhanhq import DhanContext, MarketFeed, dhanhq

from strategy import PriceActionStrategy
from trend_reversal import TrendReversalStrategy
from breakout_breakdown import BreakoutBreakdownStrategy
from ai_signal import AISignalEngine

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

FEED_SEGMENTS = {
    "IDX_I": MarketFeed.IDX,
    "NSE_EQ": MarketFeed.NSE,
    "NSE_FNO": MarketFeed.NSE_FNO,
}

INDEX_ALIASES = {
    "NIFTY": "13",
    "NIFTY 50": "13",
    "NIFTY50": "13",
    "BANKNIFTY": "25",
    "BANK NIFTY": "25",
    "FINNIFTY": "27",
    "MIDCPNIFTY": "442",
    "SENSEX": "51",
}

OPTIONABLE_INDEXES = {
    "NIFTY": ("NSE_FNO", "NIFTY"),
    "NIFTY 50": ("NSE_FNO", "NIFTY"),
    "NIFTY50": ("NSE_FNO", "NIFTY"),
    "BANKNIFTY": ("NSE_FNO", "BANKNIFTY"),
    "BANK NIFTY": ("NSE_FNO", "BANKNIFTY"),
    "SENSEX": ("BSE_FNO", "SENSEX"),
}

TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60}

# Canonical index name -> INDEX_ALIASES key. Keys are matched as a prefix so we
# can normalize Dhan-style artifacts (e.g. index-future symbols) and stray
# inputs like "BANKNIFTYIFT" / "BANKNIFTY FUT" back to the index.
INDEX_FAMILY_ROOTS = {
    "NIFTY50": "NIFTY 50",
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}

# Extra suffixes (e.g. Dhan index-future naming) that should be ignored when
# they trail a known index family root.
_INDEX_ARTIFACTS = {"IFT", "FUT", "FUTURES", "IDX", "INDEX"}

_master_cache: pd.DataFrame | None = None


def _normalize_index_query(query: str) -> str:
    """Map a raw input to a canonical INDEX_ALIASES key when possible.

    Handles spaces ("BANK NIFTY" -> "BANKNIFTY") and trailing artifacts such as
    Dhan's "IFT" index-future suffix ("BANKNIFTYIFT" -> "BANKNIFTY").
    """
    q = query.strip().upper().replace(" ", "")
    if q in INDEX_ALIASES:
        return q
    for root, alias in INDEX_FAMILY_ROOTS.items():
        if q.startswith(root):
            suffix = q[len(root):]
            if not suffix or suffix in _INDEX_ARTIFACTS:
                return alias
    return q


def _security_master() -> pd.DataFrame:
    global _master_cache
    if _master_cache is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _master_cache = dhanhq.fetch_security_list("compact")
    return _master_cache


def resolve_symbol(symbol: str) -> dict[str, Any]:
    """Resolve a symbol to Dhan instrument details.

    Returns a dict with ``security_id``, ``feed_segment`` (MarketFeed
    numeric segment), ``api_segment`` (REST segment constant),
    ``instrument_type`` and a display name.

    Raises:
        ValueError: when the symbol cannot be resolved.
    """
    query = _normalize_index_query(symbol).strip().upper()

    if query in INDEX_ALIASES:
        sid = INDEX_ALIASES[query]
        opt_seg, opt_under = OPTIONABLE_INDEXES.get(query, (None, None))
        return {
            "security_id": sid,
            "display": query,
            "feed_segment": FEED_SEGMENTS["IDX_I"],
            "feed_segment_name": "IDX_I",
            "api_segment": dhanhq.NSE,
            "instrument_type": "INDEX",
            "option_segment": opt_seg,
            "option_underlying": opt_under,
        }

    master = _security_master()
    eq = master[
        (master["SEM_INSTRUMENT_NAME"].astype(str).str.upper() == "EQUITY")
        & (master["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE")
    ]
    match = eq[
        (eq["SEM_TRADING_SYMBOL"].astype(str).str.upper() == query)
        | (eq["SEM_CUSTOM_SYMBOL"].astype(str).str.upper() == query)
    ]
    if match.empty:
        match = eq[
            eq["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.contains(query, na=False)
            | eq["SEM_CUSTOM_SYMBOL"].astype(str).str.upper().str.contains(query, na=False)
        ]
    if match.empty:
        raise ValueError(
            f"Could not resolve '{symbol}' as an NSE equity (try e.g. RELIANCE, TCS, or NIFTY 50)."
        )

    row = match.iloc[0]
    return {
        "security_id": str(row["SEM_SMST_SECURITY_ID"]),
        "display": str(row["SEM_TRADING_SYMBOL"]),
        "feed_segment": FEED_SEGMENTS["NSE_EQ"],
        "feed_segment_name": "NSE_EQ",
        "api_segment": dhanhq.NSE,
        "instrument_type": "EQUITY",
        "option_segment": None,
        "option_underlying": None,
    }


def _dhan_public_ip() -> str:
    """Best-effort public IP lookup so it can be added to the DhanHQ whitelist."""
    try:
        import httpx

        with httpx.Client(timeout=5) as client:
            r = client.get("https://api.ipify.org")
            if r.status_code == 200:
                ip = (r.text or "").strip()
                if ip:
                    return ip
    except Exception:  # noqa: BLE001 - offline / blocked: message without IP
        pass
    return ""


_DHAN_ERROR_TIPS: dict[str, str] = {
    "DH-905": "DhanHQ rejected the request because your public IP is not whitelisted",
    "DH-1004": "insufficient margin / funds in your Dhan account",
    "DH-1005": "insufficient funds",
    "DH-200": "unauthorized - check DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in .env",
    "DH-404": "requested resource/security not found",
}


def _dhan_error_text(payload: Any) -> str:
    """Turn a raw dhanhq error payload into a readable, actionable message."""
    if isinstance(payload, dict):
        code = str(payload.get("error_code") or payload.get("code") or "")
        msg = str(payload.get("error_message") or payload.get("message") or "").strip()
    else:
        code, msg = "", str(payload)
    low = (msg + " " + code).lower()
    if code == "DH-905" or "invalid ip" in low:
        tip = _DHAN_ERROR_TIPS["DH-905"]
        ip = _dhan_public_ip()
        if ip:
            tip += f". Your public IP is {ip}"
        return f"{code or 'DH-905'} {msg or 'Invalid IP'} - {tip}. Add the IP in DhanHQ console -> API/Broker settings and retry."
    if code in _DHAN_ERROR_TIPS:
        return f"{code} {msg} - {_DHAN_ERROR_TIPS[code]}".strip()
    if code or msg:
        return f"{code} {msg}".strip()
    return str(payload)


class LiveSignalEngine:
    """Streams Dhan ticks, builds candles and emits price-action signals."""

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
        client_id = os.environ.get("DHAN_CLIENT_ID")
        access_token = os.environ.get("DHAN_ACCESS_TOKEN")
        if not client_id or not access_token:
            raise ValueError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set in .env")
        self.context = DhanContext(client_id, access_token)
        self.dhan = dhanhq(self.context)

        self.symbol = symbol
        self.timeframe = timeframe
        self.minutes = TIMEFRAME_MINUTES[timeframe]
        self.seed_days = int(seed_days)
        sl = strategy.lower()
        if sl in ("trend", "trend reversal", "trend_reversal"):
            self.strategy = TrendReversalStrategy()
        elif "breakout" in sl:
            self.strategy = BreakoutBreakdownStrategy()
        else:
            self.strategy = PriceActionStrategy(
                timeframe, fast_ema, slow_ema, pivot_left, pivot_right
            )

        info = resolve_symbol(symbol)
        self.security_id = info["security_id"]
        self.display = info["display"]
        self.feed_segment = info["feed_segment"]
        self.feed_segment_name = info["feed_segment_name"]
        self.api_segment = info["api_segment"]
        self.instrument_type = info["instrument_type"]
        self.option_segment = info.get("option_segment")
        self.option_underlying = info.get("option_underlying")
        self._opt_cache: dict[str, Any] | None = None

        self.lock = threading.Lock()
        self.history: pd.DataFrame | None = None
        self.current: dict[str, Any] | None = None
        self._feed: MarketFeed | None = None
        self._thread: threading.Thread | None = None
        self.status = "not started"
        self.error = ""
        self.last_tick_at: datetime | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_closed: dict[str, Any] | None = None
        self.placed_fingerprint: str | None = None
        self.last_order_text = ""

        self.ai_engine = AISignalEngine()
        self._ai_key = None
        self._ai_value: dict[str, Any] | None = None
        self._ai_computing = False
        self._ai_history: list[dict[str, Any]] = []
        self._ai_llm_applied: tuple[str, str] = ("", "")
        self._ai_force = False

    # ------------------------------------------------------------------ boot
    def start(self) -> str:
        """Seed history, open the WebSocket and return a status message."""
        try:
            self._seed()
            self._open_stream()
            self.status = (
                f"LIVE {self.display} ({self.feed_segment_name} #{self.security_id}) "
                f"{self.timeframe} | seeded {len(self.history) if self.history is not None else 0} candles"
            )
            return self.status
        except Exception as exc:  # noqa: BLE001 - surfaced to UI
            self.error = str(exc)
            self.status = f"error: {exc}"
            return self.status

    def stop(self) -> None:
        feed = self._feed
        if feed is not None:
            try:
                feed._running = False
            except Exception:  # noqa: BLE001
                pass
            threading.Thread(
                target=self._safe_close_feed, args=(feed,), daemon=True
            ).start()
        self._feed = None
        self._thread = None
        self.status = "stopped"

    @staticmethod
    def _safe_close_feed(feed) -> None:
        time.sleep(0.1)
        try:
            feed.close_connection()
        except Exception:  # noqa: BLE001
            pass

    def _seed(self) -> None:
        to = datetime.now(IST)
        fro = to - timedelta(days=self.seed_days)
        response = self.dhan.intraday_minute_data(
            security_id=self.security_id,
            exchange_segment=self.api_segment,
            instrument_type=self.instrument_type,
            from_date=fro.strftime("%Y-%m-%d 09:15:00"),
            to_date=to.strftime("%Y-%m-%d %H:%M:%S"),
            interval=self.minutes,
            oi=False,
        )
        if response.get("status") != "success":
            raise ValueError(response.get("remarks") or "intraday seed failed")

        candles = response.get("data") or {}
        rows = []
        for i in range(len(candles.get("open", []))):
            rows.append(
                {
                    "timestamp": pd.to_datetime(
                        self.dhan.convert_to_date_time(candles["timestamp"][i])
                    ),
                    "open": float(candles["open"][i]),
                    "high": float(candles["high"][i]),
                    "low": float(candles["low"][i]),
                    "close": float(candles["close"][i]),
                    "volume": float(candles["volume"][i]),
                }
            )
        if not rows:
            raise ValueError(
                f"No historical candles for {self.display}. "
                "Market may be closed or the symbol has no trading days in the seed window."
            )
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
        df = df[df["timestamp"] < datetime.now(IST)].reset_index(drop=True)
        self.history = df

    # ------------------------------------------------------------------ feed
    def _open_stream(self) -> None:
        instruments = [
            (self.feed_segment, self.security_id, MarketFeed.Quote)
        ]
        feed = MarketFeed(
            self.context,
            instruments,
            version="v2",
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_error=self._on_error,
        )
        self._feed = feed
        self._thread = threading.Thread(target=feed.run, daemon=True)
        self._thread.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _on_connect(self, instance) -> None:
        self.status = (
            f"LIVE {self.display} ({self.feed_segment_name} #{self.security_id}) "
            f"{self.timeframe} | stream connected, waiting for ticks"
        )

    def _on_error(self, instance, error) -> None:
        friendly = _dhan_error_text(error)
        self.error = friendly
        self.status = f"error: {friendly}"

    def _watchdog(self) -> None:
        """Flag a silently-stuck connection (open socket, no ticks, no error)."""
        time.sleep(20)
        feed = self._feed
        if feed is None or feed is not self._feed:
            return
        if self.last_tick_at is None and not self.error:
            ip = _dhan_public_ip()
            hint = f" (server public IP: {ip})" if ip else ""
            self.error = (
                "No ticks received 20s after connecting - this usually means the "
                f"server's public IP is not whitelisted in the DhanHQ console{hint}, "
                "or the market is closed."
            )

    def _on_message(self, instance, msg: dict) -> Any:
        if not isinstance(msg, dict) or msg.get("type") != "Quote Data":
            return
        try:
            ltp = float(msg["LTP"])
        except (TypeError, ValueError):
            return
        with self.lock:
            now = datetime.now(IST)
            self.last_tick_at = now
            self.error = ""
            volume_now = msg.get("volume") or 0
            bucket_start = self._bucket_start(now)
            if self.current is None or bucket_start > self.current["start"]:
                self._rollover()
                self.current = {
                    "start": bucket_start,
                    "timestamp": bucket_start + timedelta(minutes=self.minutes),
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume_base": float(volume_now),
                    "_vol_at_open": float(volume_now),
                    "ticks": 1,
                }
            else:
                c = self.current
                c["high"] = max(c["high"], ltp)
                c["low"] = min(c["low"], ltp)
                c["close"] = ltp
                c["ticks"] += 1

    def _bucket_start(self, dt: datetime) -> datetime:
        minutes_of_day = dt.hour * 60 + dt.minute
        bucket = (minutes_of_day // self.minutes) * self.minutes
        return dt.replace(hour=bucket // 60, minute=bucket % 60, second=0, microsecond=0)

    def _rollover(self) -> None:
        if self.current is None:
            return
        c = self.current
        if self.history is None:
            return
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

    # ------------------------------------------------------------------ read
    def snapshot(self) -> tuple[dict[str, Any] | None, pd.DataFrame | None]:
        """Return the latest signal summary and the display DataFrame.

        Signals are always evaluated on *completed* candles only. The
        in-progress live candle is appended for display but never drives a
        signal.
        """
        with self.lock:
            if self.history is None:
                return None, None

            closed = self.strategy.generate_signal(self.history.copy())
            latest_closed = closed.iloc[-1]

            display = closed
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
                            "trend": "",
                            "signal": "FORMING",
                            "reason": "in-progress live candle",
                            "position": "",
                        }
                    ]
                )
                for col in ["ema_fast", "ema_slow", "support", "resistance"]:
                    if col not in current_row:
                        current_row[col] = float("nan")
                display = pd.concat([closed, current_row], ignore_index=True)

            frame = display
            last = frame.iloc[-1]
            f = lambda x: float(x) if pd.notna(x) else float("nan")  # noqa: E731
            summary = {
                "display": self.display,
                "timeframe": self.timeframe,
                "live_price": f(last.close),
                "candle_time": (
                    str(last.timestamp) if "timestamp" in frame.columns else ""
                ),
                "trend": str(latest_closed.trend),
                "signal": str(latest_closed.signal),
                "reason": str(latest_closed.reason),
                "position": str(latest_closed.position),
                "support": f(latest_closed.support),
                "resistance": f(latest_closed.resistance),
                "ema_fast": f(latest_closed.ema_fast),
                "ema_slow": f(latest_closed.ema_slow),
                "seed_days": self.seed_days,
                "closes_at": (
                    str(self.current["timestamp"]) if self.current else "n/a"
                ),
            }
            self._last_snapshot = summary
            self._last_closed = {
                "timestamp": str(latest_closed.timestamp),
                "signal": str(latest_closed.signal),
                "price": float(latest_closed.close),
                "reason": str(latest_closed.reason),
                "position": str(latest_closed.position),
            }
            return summary, display

    # ------------------------------------------------------------------- AI
    def set_llm(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Select an LLM provider/model from the UI; forces the next AI pass.

        The verdict is recomputed the next time ``ai_signal`` is polled after
        the selection changes.
        """
        sig = ((provider or "").strip(), (model or "").strip(), (api_key or "").strip())
        if sig != self._ai_llm_applied:
            self.ai_engine.set_llm(provider=provider, model=model, api_key=api_key)
            self._ai_llm_applied = sig
            self._ai_force = True

    def ai_signal(self, force: bool = False) -> dict[str, Any]:
        """AI verdict on the latest closed candle (computed once per candle).

        Heavy model fitting is skipped while an earlier analysis is still
        running; callers poll again on the next tick for the cached result.
        ``force=True`` (e.g. after a provider/model change) recomputes now.
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
        except Exception as exc:  # noqa: BLE001 - surfaced to UI
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

    # ------------------------------------------------------------------ orders
    def trade_preview(self, quantity: int = 5, sl_pct: float = 2.0) -> str:
        """Build a human-readable preview for the current BUY/SELL signal."""
        with self.lock:
            lc = self._last_closed
            if lc is None or lc["signal"] not in ("BUY", "SELL"):
                return "No trade signal yet (latest closed candle is HOLD)."
            price = lc["price"]
            qty = max(1, int(quantity))
            sl = self._sl_price(price, lc["signal"], sl_pct)
            notional = price * qty
            lines = [
                "--- ORDER PREVIEW (manual confirm) ---",
                f"Security:    {self.display} ({self.feed_segment_name} #{self.security_id})",
                f"Signal time: {lc['timestamp']}",
                f"Action:      {lc['signal']}   ({lc['reason']})",
                f"Quantity:    {qty}",
                f"Entry:       LIMIT @ {price:.2f}",
                f"Stop-loss:   SLM @ trigger {sl:.2f}",
                f"Product:     INTRADAY (DAY)",
                f"Notional:    Rs. {notional:,.2f}",
            ]
            if self._last_snapshot is not None:
                lines.append(f"Live now:    {self._last_snapshot['live_price']:.2f}")
            if notional > 50000:
                lines.append("WARNING:     Notional exceeds Rs. 50,000")
            fp = f"{lc['timestamp']}|{lc['signal']}"
            if fp == self.placed_fingerprint:
                lines.append("NOTE:        this signal was already placed earlier")
            lines.append("---------------------")
            return "\n".join(lines)

    @staticmethod
    def _sl_price(price: float, signal: str, sl_pct: float) -> float:
        if signal == "BUY":
            return round(price * (1 - abs(sl_pct) / 100.0), 2)
        return round(price * (1 + abs(sl_pct) / 100.0), 2)

    def place_signal_order(self, quantity: int = 5, sl_pct: float = 2.0) -> str:
        """Place the current BUY/SELL signal as an entry LIMIT + stop-loss.

        Manual flow: the caller shows ``trade_preview`` first and the user
        confirms before this runs. Entry always goes first; the stop-loss
        leg is only sent if the entry leg was accepted.
        """
        with self.lock:
            lc = self._last_closed
            if lc is None or lc["signal"] not in ("BUY", "SELL"):
                return "No BUY/SELL signal to place."
            qty = int(quantity)
            if qty < 1:
                return "quantity must be at least 1"
            price = lc["price"]
            action = dhanhq.BUY if lc["signal"] == "BUY" else dhanhq.SELL
            sl_trigger = self._sl_price(price, lc["signal"], sl_pct)
            sl_action = dhanhq.SELL if action == dhanhq.BUY else dhanhq.BUY
            fp = f"{lc['timestamp']}|{lc['signal']}"
            candle_time = lc["timestamp"]

        tag = f"pat_{action.lower()}_{int(time.time())}"
        report = []
        entry = self.dhan.place_order(
            security_id=self.security_id,
            exchange_segment=self.api_segment,
            transaction_type=action,
            quantity=qty,
            order_type=dhanhq.LIMIT,
            product_type=dhanhq.INTRA,
            price=price,
            trigger_price=0,
            validity=dhanhq.DAY,
            tag=tag,
        )
        report.append("ENTRY LIMIT: " + self._fmt_order(entry))

        if entry.get("status") == "success" and entry.get("data", {}).get("orderId"):
            sl = self.dhan.place_order(
                security_id=self.security_id,
                exchange_segment=self.api_segment,
                transaction_type=sl_action,
                quantity=qty,
                order_type=dhanhq.SLM,
                product_type=dhanhq.INTRA,
                price=0,
                trigger_price=sl_trigger,
                validity=dhanhq.DAY,
                tag=f"pat_sl_{int(time.time())}",
            )
            report.append(f"STOP-LOSS SLM @ {sl_trigger:.2f}: " + self._fmt_order(sl))
        else:
            report.append("STOP-LOSS skipped: entry leg was not accepted (see ENTRY LIMIT above).")

        with self.lock:
            self.placed_fingerprint = fp
            self.last_order_text = "\n".join(report)
        return (
            f"PLACED for signal @ {candle_time}\n"
            f"{self.last_order_text}"
        )

    @staticmethod
    def _fmt_order(response: dict[str, Any]) -> str:
        if response.get("status") == "success":
            data = response.get("data") or {}
            return f"OK orderId={data.get('orderId', 'n/a')} status={data.get('orderStatus')}"
        return "FAILED: " + _dhan_error_text(response.get("remarks") or response)

    # ------------------------------------------------------------- index options
    def option_choices(self) -> tuple[list[dict[str, Any]], str | None, float | None]:
        """Return 3 option strikes sized to the current signal.

        BUY  -> 3 Call (CE) strikes near the spot (ATM/OTM bias).
        SELL -> 3 Put  (PE) strikes near the spot (ATM/OTM bias).

        Works only for NIFTY / BANKNIFTY / SENSEX. Returns
        ``(entries, expiry, spot)`` where each entry has security_id,
        strike, option_type, premium, lot_size, moneyness and a label.
        """
        with self.lock:
            lc = self._last_closed
            live = self._last_snapshot
            if self.option_segment is None or lc is None or lc["signal"] not in ("BUY", "SELL"):
                return [], None, None
            spot = float((live or {}).get("live_price")) if live is not None else float(lc["price"])
            side = "CE" if lc["signal"] == "BUY" else "PE"

            if self._opt_cache is not None:
                c = self._opt_cache
                if (
                    c["side"] == side
                    and time.time() - c["at"] < 25
                    and abs(c["spot"] - spot) / spot < 0.02
                ):
                    return c["entries"], c["expiry"], spot

        expiry, strikes, side_df = self._selection_for(side, spot)
        if not strikes:
            return [], None, spot

        candidates = sorted(strikes, key=lambda s: abs(s - spot))[:9]
        rows = side_df[side_df["SEM_STRIKE_PRICE"].astype(float).isin(candidates)]
        rows = rows.drop_duplicates(subset=["SEM_STRIKE_PRICE"])
        ids = [int(r["SEM_SMST_SECURITY_ID"]) for _, r in rows.iterrows()]

        premiums: dict[str, float] = {}
        for attempt in range(2):
            seg_data = {}
            try:
                quote = self.dhan.ticker_data({self.option_segment: ids})
                payload = (quote.get("data") or {}).get("data") or {}
                seg_data = payload.get(self.option_segment) or {}
            except Exception:  # noqa: BLE001 - rate limit / transient
                seg_data = {}
            if seg_data:
                for sid_key, q in seg_data.items():
                    try:
                        premiums[str(sid_key)] = float(q["last_price"])
                    except (TypeError, ValueError, KeyError):
                        premiums[str(sid_key)] = 0.0
                break
            time.sleep(1.0)

        entries = []
        for _, r in rows.iterrows():
            strike = float(r["SEM_STRIKE_PRICE"])
            sid = str(r["SEM_SMST_SECURITY_ID"])
            lot = int(float(r["SEM_LOT_UNITS"]) or 1)
            prem = premiums.get(sid, 0.0)
            if prem <= 0:
                continue
            if side == "CE":
                mm = "OTM" if strike > spot else ("ATM" if strike == spot else "ITM")
            else:
                mm = "ITM" if strike > spot else ("ATM" if strike == spot else "OTM")
            entries.append(
                {
                    "security_id": sid,
                    "strike": strike,
                    "option_type": side,
                    "premium": prem,
                    "lot_size": lot,
                    "moneyness": mm,
                    "segment": self.option_segment,
                    "label": (
                        f"{self.option_underlying} {strike:,.0f} {side} "
                        f"@ {prem:.2f} ({mm})"
                    ),
                }
            )
        if side == "CE":
            entries.sort(key=lambda e: (e["strike"] < spot, abs(e["strike"] - spot)))
        else:
            entries.sort(key=lambda e: (e["strike"] > spot, abs(e["strike"] - spot)))
        entries = entries[:3]
        self._opt_cache = {
            "side": side,
            "spot": spot,
            "at": time.time(),
            "expiry": expiry,
            "entries": entries,
        }
        return entries, expiry, spot

    def _selection_for(self, side: str, spot: float) -> tuple[str | None, list[float], pd.DataFrame]:
        master = _security_master()
        instr = master["SEM_INSTRUMENT_NAME"].astype(str).str.upper()
        cs = master["SEM_CUSTOM_SYMBOL"].astype(str).str.upper()
        opt = master[(instr == "OPTIDX") & cs.str.startswith(self.option_underlying + " ")].copy()
        exp = pd.to_datetime(opt["SEM_EXPIRY_DATE"], errors="coerce").dt.date
        opt["_exp"] = exp
        today = datetime.now(IST).date()
        offered = sorted({d for d in exp.dropna().unique()})
        if not offered:
            return None, [], opt.iloc[0:0]
        chosen_exp = min(offered, key=lambda d: (d < today, d))
        opt = opt[opt["_exp"] == chosen_exp]
        side_df = opt[opt["SEM_OPTION_TYPE"].astype(str).str.upper() == side]
        strikes = sorted(float(s) for s in side_df["SEM_STRIKE_PRICE"].astype(float).unique())
        return str(chosen_exp), strikes, side_df

    def place_option_order(
        self, security_id: str, lots: int = 1, sl_pct: float = 2.0
    ) -> str:
        """Buy the selected option strike (LIMIT) with a stop-loss on the premium.

        BUY signal -> the dropdown only carries Call strikes; SELL -> Puts.
        Quantity = lots x contract lot size. Product is INTRADAY (FNO).
        """
        with self.lock:
            lc = self._last_closed
            if self.option_segment is None:
                return "Option trading is only available for NIFTY / BANKNIFTY / SENSEX."
            if lc is None or lc["signal"] not in ("BUY", "SELL"):
                return "No BUY/SELL signal for an option order."

        entries, expiry, _spot = self.option_choices()
        entry = next((e for e in entries if e["security_id"] == str(security_id)), None)
        if entry is None:
            return "Selected strike not found - refresh the preview and pick again."
        with self.lock:
            want = "CE" if self._last_closed["signal"] == "BUY" else "PE"
        if entry["option_type"] != want:
            return f"Only {want} strikes for a {self._last_closed['signal']} signal."

        qty = int(lots) * entry["lot_size"]
        if qty < 1:
            return "lots must be at least 1"
        premium = float(entry["premium"])
        if premium <= 0:
            return "No live premium for this strike - pick another strike."
        sl_trigger = round(premium * (1 - abs(sl_pct) / 100.0), 2)
        seg = dhanhq.NSE_FNO if entry["segment"] == "NSE_FNO" else dhanhq.BSE_FNO
        sign = self.option_underlying
        tag = f"pat_{sign.lower()}_{int(time.time())}"

        report = [
            "--- OPTION ORDER (manual confirm) ---",
            f"Security:   {sign} {entry['strike']:,.0f} {entry['option_type']} ({entry['moneyness']})",
            f"Exchange:   {entry['segment']} (id {entry['security_id']})",
            f"Expiry:     {expiry}",
            f"Action:     BUY {entry['option_type']}",
            f"Quantity:   {lots} lot x {entry['lot_size']} = {qty}",
            f"Entry:      LIMIT @ premium {premium:.2f}",
            f"Stop-loss:  SLM @ {sl_trigger:.2f}",
            f"Product:    INTRADAY (DAY)",
            f"Notional:   Rs. {premium * qty:,.2f}",
        ]
        notional = premium * qty
        if notional > 50000:
            report.append("WARNING:    Notional exceeds Rs. 50,000")
        report.append("---------------------")

        r1 = self.dhan.place_order(
            security_id=str(entry["security_id"]),
            exchange_segment=seg,
            transaction_type=dhanhq.BUY,
            quantity=qty,
            order_type=dhanhq.LIMIT,
            product_type=dhanhq.INTRA,
            price=premium,
            trigger_price=0,
            validity=dhanhq.DAY,
            tag=tag,
        )
        report.append("ENTRY LIMIT: " + self._fmt_order(r1))
        if r1.get("status") == "success" and r1.get("data", {}).get("orderId"):
            r2 = self.dhan.place_order(
                security_id=str(entry["security_id"]),
                exchange_segment=seg,
                transaction_type=dhanhq.SELL,
                quantity=qty,
                order_type=dhanhq.SLM,
                product_type=dhanhq.INTRA,
                price=0,
                trigger_price=sl_trigger,
                validity=dhanhq.DAY,
                tag=f"{sign.lower()}_sl_{int(time.time())}",
            )
            report.append(f"STOP-LOSS SLM @ {sl_trigger:.2f}: " + self._fmt_order(r2))
        else:
            report.append("STOP-LOSS skipped: entry leg was not accepted (see ENTRY LIMIT above).")

        with self.lock:
            self.placed_fingerprint = (
                f"{self._last_closed['timestamp']}|{self._last_closed['signal']}|opt"
            )
            self.last_order_text = "\n".join(report)
        return "\n".join(report)