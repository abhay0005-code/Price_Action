"""Live Alpha Vantage data feed for the Price Action Trading Terminal.

Mirrors the ``FinnhubLiveEngine`` interface so the same Gradio UI patterns
can be reused for the US tab:

- resolves a symbol to an Alpha Vantage instrument:
  * US equities / ETFs (AAPL, TSLA, SPY, ...)
  * forex pairs (EURUSD, GBPUSD, USDINR, ...)
  * gold & silver spot (GOLD / XAU, SILVER / XAG)
- seeds the strategy with recent intraday candles via yfinance (Alpha
  Vantage intraday FX/metals data is premium-only; equities use its free
  ``TIME_SERIES_INTRADAY`` endpoint)
- polls the matching free real-time endpoint on a background thread
  (Alpha Vantage has no free WebSocket) and builds timeframe candles:
  ``GLOBAL_QUOTE`` for equities, ``CURRENCY_EXCHANGE_RATE`` for forex,
  ``GOLD_SILVER_SPOT`` for gold/silver
- recomputes price-action signals on every closed candle

Paper-trade only - no US broker is integrated.  The "order preview" shows a
paper trade instruction.

Credentials are read from ``.env``:
- ``ALPHAVANTAGE_API_KEY`` - Alpha Vantage API key (free tier works:
  https://www.alphavantage.co/support/#api-key)

Note: free Alpha Vantage keys are now capped at 25 requests/day, so keep
``ALPHAVANTAGE_POLL_SECONDS`` high and only run one feed at a time - the
14s-or-faster polling of a single stream will otherwise exhaust the daily
quota within minutes.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from finnhub_feed import FinnhubLiveEngine, IST, UTC

NY = ZoneInfo("America/New_York")

AV_BASE_URL = "https://www.alphavantage.co/query"

# Physical fiat currency codes accepted by Alpha Vantage FX / exchange-rate
# endpoints.  A six-letter symbol whose two halves are both in this set is
# treated as a forex pair (e.g. EURUSD, USDINR).
FIAT_CURRENCIES = frozenset({
    "AED", "ARS", "AUD", "BHD", "BRL", "CAD", "CHF", "CLP", "CNY", "COP",
    "CZK", "DKK", "EGP", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR",
    "ISK", "JPY", "JOD", "KES", "KRW", "KWD", "LKR", "MAD", "MXN", "MYR",
    "NGN", "NOK", "NZD", "OMR", "PHP", "PKR", "PLN", "QAR", "RON", "RUB",
    "SAR", "SEK", "SGD", "THB", "TRY", "TWD", "UAH", "USD", "UYU", "VND",
    "ZAR",
})

GOLD_SYMBOLS = frozenset({
    "GOLD", "XAU", "XAUUSD", "XAU/USD", "XAU-USD", "XAU.USD", "GOLDUSD",
    "GOLD/USD", "GOLD-USD", "GC=F",
})

SILVER_SYMBOLS = frozenset({
    "SILVER", "XAG", "XAGUSD", "XAG/USD", "XAG-USD", "XAG.USD", "SILVERUSD",
    "SILVER/USD", "SILVER-USD", "SI=F",
})

# Crude oil futures/spot aliases (WTI + Brent). Live ticks come from the free
# Alpha Vantage COMMODITIES endpoint (daily granularity, values in USD) and the
# intraday seed falls back to Yahoo front-month futures (CL=F / BZ=F).
WTI_SYMBOLS = frozenset({
    "WTI", "WTIUSD", "WTI/USD", "WTI-USD", "CRUDE", "CRUDEOIL", "CRUDE_OIL",
    "CRUDE-OIL", "CRUDE/USD", "OIL", "USOIL", "USOIL/USD", "CL=F", "CL",
})
BRENT_SYMBOLS = frozenset({
    "BRENT", "BRENTUSD", "BRENT/USD", "BRENT-USD", "BZ=F", "BZ", "COIL",
    "UKOIL", "XBR",
})


def _parse_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_av_symbol(symbol: str) -> dict[str, Any]:
    """Resolve a ticker to an Alpha Vantage instrument.

    Returns a dict with the keys consumed by ``FinnhubLiveEngine``
    (``symbol`` / ``display`` / ``instrument_type``) plus AV-specific
    metadata used for public health checks of the quote endpoints.
    """
    query = symbol.strip().upper()
    if not query:
        raise ValueError("symbol must not be empty")
    cleaned = query.replace("/", "").replace("-", "").replace(".", "").replace("_", "")
    if cleaned in GOLD_SYMBOLS or query in GOLD_SYMBOLS:
        return {
            "symbol": "XAU",
            "display": "XAU/USD",
            "instrument_type": "METALS",
            "av_symbol": "GOLD",
            "av_from": "XAU",
            "av_to": "USD",
        }
    if cleaned in SILVER_SYMBOLS or query in SILVER_SYMBOLS:
        return {
            "symbol": "XAG",
            "display": "XAG/USD",
            "instrument_type": "METALS",
            "av_symbol": "XAG",
            "av_from": "XAG",
            "av_to": "USD",
        }
    if cleaned in WTI_SYMBOLS or query in WTI_SYMBOLS:
        return {
            "symbol": "WTI",
            "display": "WTI Crude Oil (USD/bbl)",
            "instrument_type": "COMMODITY",
            "av_symbol": "wti_crude_oil",
            "yf_tickers": ["CL=F"],
        }
    if cleaned in BRENT_SYMBOLS or query in BRENT_SYMBOLS:
        return {
            "symbol": "BRENT",
            "display": "Brent Crude Oil (USD/bbl)",
            "instrument_type": "COMMODITY",
            "av_symbol": "brent_crude_oil",
            "yf_tickers": ["BZ=F"],
        }
    if (
        len(cleaned) == 6
        and cleaned[:3] in FIAT_CURRENCIES
        and cleaned[3:] in FIAT_CURRENCIES
    ):
        base, quote = cleaned[:3], cleaned[3:]
        return {
            "symbol": f"{base}{quote}",
            "display": f"{base}/{quote}",
            "instrument_type": "FOREX",
            "av_from": base,
            "av_to": quote,
        }
    return {
        "symbol": query,
        "display": query,
        "instrument_type": "US_EQUITY",
    }


class AlphaVantageLiveEngine(FinnhubLiveEngine):
    """Streams Alpha Vantage ticks (REST polling), builds candles and emits signals."""

    provider_name = "alphavantage"
    seed_provider = "alphavantage"

    INTRADAY_INTERVALS = {5: "5min", 15: "15min", 60: "60min"}

    def _validate_credentials(self) -> None:
        api_key = (os.environ.get("ALPHAVANTAGE_API_KEY") or "").strip()
        if not api_key:
            raise ValueError(
                "ALPHAVANTAGE_API_KEY not set in .env. "
                "Get a free key at https://www.alphavantage.co/support/#api-key"
            )
        self._api_key = api_key

    def _make_feed_client(self):
        """Build the REST client used for history seeding and live polling."""
        self._http = httpx.Client(timeout=12.0)
        self._last_info = ""
        return None

    # ---------------------------------------------------------------- symbol

    def _resolve_symbol(self, symbol: str) -> dict[str, Any]:
        """Override: classify US equity / forex pair / metal / commodity."""
        resolved = _resolve_av_symbol(symbol)
        self._av_symbol = resolved.get("av_symbol")
        self._av_from = resolved.get("av_from")
        self._av_to = resolved.get("av_to")
        self._yf_override = list(resolved.get("yf_tickers") or [])
        return resolved

    def _yf_tickers(self) -> list[str]:
        """Yahoo tickers for the intraday seed fallback (jour FX is premium)."""
        if self.instrument_type == "COMMODITY":
            return list(getattr(self, "_yf_override", None) or [])
        if self.instrument_type == "METALS":
            return ["GC=F" if self._av_symbol == "GOLD" else "SI=F"]
        if self.instrument_type == "FOREX":
            if self._av_from == "USD":
                return [f"{self._av_to}=X", f"USD{self._av_to}=X"]
            return [f"{self._av_from}{self._av_to}=X", f"{self._av_to}=X"]
        return [self.symbol]

    # ---------------------------------------------------------------- helpers

    def _av_get(self, params: dict[str, Any]) -> dict:
        """Query the Alpha Vantage REST API; {} on network/HTTP errors.

        Top-level ``Information`` (quota exhausted, invalid key...) is stored
        in ``self._last_info`` so callers can surface it to the user.
        """
        try:
            resp = self._http.get(
                AV_BASE_URL, params={**params, "apikey": self._api_key}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        self._last_info = str(data.get("Information") or "")
        return data

    def _quote_params(self) -> tuple[dict[str, Any], str, str]:
        """Endpoint params + wrapper/price keys for the current instrument."""
        if self.instrument_type == "FOREX":
            return (
                {
                    "function": "CURRENCY_EXCHANGE_RATE",
                    "from_currency": self._av_from,
                    "to_currency": self._av_to,
                },
                "Realtime Currency Exchange Rate",
                "5. Exchange Rate",
            )
        if self.instrument_type == "COMMODITY":
            # Free COMMODITIES endpoint - queried by _global_quote override.
            return (
                {"function": "COMMODITIES", "commodity": self._av_symbol},
                "",
                "price",
            )
        if self.instrument_type == "METALS":
            return (
                {"function": "GOLD_SILVER_SPOT", "symbol": self._av_symbol},
                "",
                "price",
            )
        return (
            {"function": "GLOBAL_QUOTE", "symbol": self.symbol},
            "Global Quote",
            "05. price",
        )

    def _global_quote(self) -> dict[str, Any]:
        """Current price (+volume for equities) for the instrument."""
        params, wrapper, price_key = self._quote_params()
        data = self._av_get(params)
        if self.instrument_type == "COMMODITY":
            # {"data": [{"date": "YYYY-MM-DD", "value": "63.21"}, ...]}
            series = data.get("data")
            if isinstance(series, list) and series and isinstance(series[0], dict):
                return {
                    "price": _parse_float(series[0].get("value")),
                    "volume": 0.0,
                }
            notice = data.get("Note") or self._last_info
            if notice:
                self.error = f"alphavantage: {notice}"
            return {"price": 0.0, "volume": 0.0}
        quote = data.get(wrapper) if wrapper else data
        if not isinstance(quote, dict):
            quote = {}
        price = _parse_float(quote.get(price_key))
        volume = (
            _parse_float(quote.get("06. volume"))
            if self.instrument_type == "US_EQUITY"
            else 0.0
        )
        return {"price": price, "volume": volume}

    # ---------------------------------------------------------------- seed

    def _seed_finnhub(self) -> list[dict] | None:
        """Historical candles via Alpha Vantage TIME_SERIES_INTRADAY.

        Only applies to US equities - forex and metals intraday data are
        premium-only on Alpha Vantage, so ``None`` makes the base class fall
        back to the keyless yfinance seed (``=X`` pairs / ``GC=F`` / ``SI=F``).
        """
        if self.instrument_type != "US_EQUITY":
            return None
        interval = self.INTRADAY_INTERVALS[self.minutes]
        data = self._av_get(
            {
                "function": "TIME_SERIES_INTRADAY",
                "symbol": self.symbol,
                "interval": interval,
                "outputsize": "compact",
            }
        )
        if not isinstance(data, dict):
            return None
        key = None
        for candidate in data:
            if str(candidate).lower().startswith("time series ("):
                key = candidate
                break
        series = data.get(key) if key else None
        if not isinstance(series, dict):
            return None

        rows = []
        for ts_str, ohlc in series.items():
            if not isinstance(ohlc, dict):
                continue
            try:
                candle_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            # Alpha Vantage intraday stamps are US/Eastern wall-clock.
            candle_time = candle_time.replace(tzinfo=NY).astimezone(UTC)
            rows.append(
                {
                    "timestamp": candle_time,
                    "open": _parse_float(ohlc.get("1. open", 0)),
                    "high": _parse_float(ohlc.get("2. high", 0)),
                    "low": _parse_float(ohlc.get("3. low", 0)),
                    "close": _parse_float(ohlc.get("4. close", 0)),
                    "volume": _parse_float(ohlc.get("5. volume", 0)),
                }
            )
        if not rows:
            return None
        rows.sort(key=lambda r: r["timestamp"])
        return rows

    # ---------------------------------------------------------------- stream

    def _open_stream(self) -> None:
        """Validate the key, then start a background REST polling pump."""
        params, _, _ = self._quote_params()
        data = self._av_get(params)
        if data.get("Error Message"):
            raise RuntimeError(
                f"Alpha Vantage rejected the API key: {data['Error Message']}"
            )
        notice = data.get("Note") or self._last_info
        if notice:
            self.error = f"alphavantage: {notice}"

        self._poll_stop = threading.Event()
        self._feed = self
        self._ws = None
        self._thread = threading.Thread(
            target=self._poll_loop, args=(self._poll_stop,), daemon=True
        )
        self._ws_ready.set()
        self._thread.start()

        label = {
            "US_EQUITY": "US/EQUITY",
            "FOREX": "FOREX",
            "METALS": "METALS",
            "COMMODITY": "COMMODITY",
        }.get(self.instrument_type, "US/EQUITY")
        self.status = (
            f"LIVE {self.display} ({label} via Alpha Vantage polling) {self.timeframe} | "
            "stream connected, waiting for ticks"
        )

    def _poll_loop(self, stop_event: threading.Event) -> None:
        interval = max(
            float(os.environ.get("ALPHAVANTAGE_POLL_SECONDS", "15") or 15), 5.0
        )
        while not stop_event.wait(interval):
            try:
                quote = self._global_quote()
            except Exception as exc:
                self.error = f"alphavantage poll: {exc}"
                continue
            if self._last_info:
                # Quota exhausted / key invalid / endpoint error - surface it
                # instead of silently polling an empty response.
                self.error = f"alphavantage: {self._last_info}"
                continue
            price = quote.get("price") or 0.0
            if price > 0:
                self._on_tick(
                    {"type": "quote", "p": price, "cp": price, "close": price}
                )

    def stop(self) -> None:
        stop_event = getattr(self, "_poll_stop", None)
        if stop_event is not None:
            stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
            self._poll_stop = None
        http = getattr(self, "_http", None)
        if http is not None:
            try:
                http.close()
            except Exception:
                pass
        self.status = "stopped"