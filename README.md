# Price Action + Support/Resistance Trading Terminal

Python + Gradio starter project for 5m / 15m / 1h price-action signals.

## Rules
- Time-based stop-and-reverse (per candle, no trend gate):
  - A closed candle that closes above the previous candle's high turns the position **LONG (BUY)**.
  - A closed candle that closes below the previous candle's low turns the position **SHORT (SELL)**.
  - Otherwise the previous position is kept (**HOLD**).
- EMA trend (fast vs slow) is displayed for direction and sets the stop-loss side;
  it does not trigger signals.
- Pivot support/resistance is displayed for context and is not a signal trigger.

### Trend Reversal strategy (multi-timeframe confluence)
Optional second strategy in both tabs. Emits BUY/SELL only when all layers align:
- **1h (bias):** 200-period EMA slope sets the macro direction.
- **15m (setup):** 15m swing support/resistance zones plus RSI divergence
  (bullish: price lower-low while RSI makes a higher low; bearish mirrored).
- **Base (5m or your finest bars, entry):** bullish/bearish **engulfing** or **pin bar**,
  a **9 EMA / 21 EMA cross**, and **RSI > 40** (buy) or **< 60** (sell).

The base series is chosen by the tab you're in (uploaded CSV / Dhan live timeframe);
15m and 1h are re-aggregated from it internally. Give it at least ~2 weeks of bars so
the 1h 200-EMA has context; signals are rare by design (a confluence strategy).

### Breakout / Breakdown strategy (multi-timeframe structure)
Third strategy in both tabs. Buys breakouts and sells breakdowns, and counter-trades
exhausted trends:
- **1h (macro):** 1h EMA sets the bias; confirmed 1h pivot highs/lows are the major
  support/resistance zones.
- **15m (middle):** close beyond the 15m swing high/low confirms the structural
  breakout/breakdown; RSI swing divergence at a 1h level flags a reversal.
- **Base (5m, entry):** a **9/21 EMA cross**, a **volume spike** (>= 1.5x the 20-bar
  average) and a **close above the 5m swing high** (buy) / below the swing low (sell)
  trigger the entry. Reversals need price to reclaim/lose both the 21 EMA and the 5m
  swing level at a 1h support/resistance with a contrary RSI divergence.

Defaults: fast/slow EMA 9/21, 1h EMA 50, RSI 14, 15m/5m swing lookbacks 6/4. Tune via
`BreakoutBreakdownStrategy(...)` in `breakout_breakdown.py`.

### AI Signal (Live tab, automatic every 5m candle)
While a live feed is running, the AI panel recomputes a verdict on every **closed 5m
candle** (no button needed). It combines:

- Indicators: **169 EMA**, 9/21/50 EMA, RSI, volatility, volume z-score, candle shape,
  range-breakout flags.
- Statistical models: **ARIMA(1,1,1)** log-price forecast, **AR(1)-GARCH(1,1)** mean +
  volatility forecast, **Kalman** local-linear-trend filter.
- **XGBoost + LSTM ensemble**: an XGBoost classifier and a small single-layer
  LSTM (via PyTorch, CPU) both classify the next-bar direction from the same
  feature windows, are trained on the actual live bars
  (features at bar `t` → sign of the return at bar `t+1`), and are retrained as
  new bars close.
- The three rule strategies (Price Action / Trend Reversal / Breakout & Breakdown).
- An optional **LLM judge**: pick provider + model in the Live tab, or set
  `LLM_PROVIDER` / `LLM_MODEL` in `.env`. Supported providers:

  | Provider | Type | API key env var | Model |
  |---|---|---|---|
  | `ollama` | local, open-source | none (run `ollama serve`) | `llama3.1:8b`, ... |
  | `huggingface` | hosted, open-source | `HF_API_TOKEN` | `meta-llama/Llama-3.1-8B-Instruct` |
  | `openrouter` | aggregator | `OPENROUTER_API_KEY` | `anthropic/claude-3.5-sonnet`, `meta-llama/...` |
  | `groq` | hosted, open-source, fast | `GROQ_API_KEY` | `llama-3.3-70b-versatile` |
  | `claude` | paid (Anthropic) | `ANTHROPIC_API_KEY` | `claude-sonnet-4-5` |
  | `chatgpt` | paid (OpenAI) | `OPENAI_API_KEY` | `gpt-4o-mini` |

  Selecting a provider populates a model dropdown; selecting a model (or
  pressing **Run AI Analysis**) re-runs the analysis and the verdict is sent to
  that model. The LLM must reply with strict JSON `{signal, confidence, reason}`.
  A custom OpenAI-compatible endpoint still works via `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`.

The final signal = weighted vote of all models + strategies with a confidence score.
It is **display only** — no orders are placed automatically.

New dependencies: `statsmodels`, `arch`, `xgboost`, `httpx` (see `requirements.txt`).

## CSV
Columns required: `timestamp,open,high,low,close,volume`

## Run on Windows
```powershell
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```

This version uses CSV data for testing. It also streams **live DhanHQ data** via WebSocket
to generate signals on real-time candles. It does not place orders.

## Live Dhan Feed

Requires valid DhanHQ credentials in `.env`:

```ini
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_access_token
```

Open the **Live Dhan Feed** tab, enter an NSE symbol (e.g. `RELIANCE`, `TCS`) or an index
(`NIFTY 50`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`), pick the timeframe and start
the feed. The app:

1. Resolves the symbol to a Dhan security ID.
2. Seeds the strategy with recent intraday candles.
3. Streams `MarketFeed` Quote ticks and builds timeframe candles.
4. Emits BUY/SELL/HOLD signals on every closed candle.

### Manual order to Dhan (no auto-trading)

When the latest closed candle produces a **BUY** or **SELL** signal, the app shows a
readable order preview (entry `LIMIT` + `SLM` stop-loss, product `INTRADAY`). You confirm
it in the UI and click **Place Order on Dhan** — nothing is placed automatically.

- Quantity and stop-loss % are adjustable in the UI.
- The stop-loss is set below the entry for BUY and above the entry for SELL.
- Warning is shown when notional exceeds Rs. 50,000.
- Order placement requires Dhan static-IP whitelisting (`DH-911` invalid IP errors are shown
  in the Order Response box). Market orders are NOT used; entry is `LIMIT`.

### Index options (NIFTY / BANKNIFTY / SENSEX)

For these three indices the Live tab also shows an option-strike picker when a **BUY**
or **SELL** signal is present:

- BUY signal → the 3 nearest Call (CE) strikes, SELL signal → the 3 nearest Put (PE) strikes.
- Strikes come from the Dhan security master (nearest expiry on/after today) and premiums
  are fetched live, so only strikes with a real quote are shown.
- Enter **Lots** (quantity = lots × contract lot size), click **Buy Selected Option** to
  place a BUY `LIMIT` on the option premium plus a `SLM` stop-loss on the premium
  (product `INTRADAY`, exchange `NSE_FNO` for NIFTY/BANKNIFTY, `BSE_FNO` for SENSEX).
- Nothing is placed automatically; every order needs the button click.

## Live US Market Feed (Finnhub / Alpaca)

Open the **Live US Market Feed** tab, pick a data provider and a US ticker
(`AAPL`, `TSLA`, `SPY`, ...), choose the timeframe and start the feed. The app:

1. Resolves the ticker.
2. Seeds the strategy with recent intraday candles (via the provider's REST API,
   with a free `yfinance` fallback when the provider cannot serve them).
3. Streams real-time trades/quotes via the provider's WebSocket and builds
   timeframe candles.
4. Emits BUY/SELL/HOLD signals on every closed candle.

Credentials live in `.env`:

```ini
# Alpaca (recommended - paper keys work, no candle limits)
APCA_API_KEY_ID=your_key_id
APCA_API_SECRET_KEY=your_secret_key
# or Finnhub
FINNHUB_API_KEY=your_finnhub_key
```

- **Alpaca** streams the real-time IEX feed over the Market Data WebSocket and
  seeds history from the Market Data REST API. Paper keys are fine:
  https://app.alpaca.markets/paper
- **Finnhub** free keys cannot call `/stock/candle` (HTTP 403), so history is
  seeded from `yfinance` instead; live ticks still stream from Finnhub.

US feeds are **paper-trade only** — no US broker is integrated, so order buttons
show a preview instruction instead of placing a real order.

Notes
- Signals are evaluated on completed candles only (per the strategy rules).
- Equities have full historical seeds from Dhan.
- Indices return only the current-day candle from Dhan's intraday API, so index signals
  warm up as candles close during the live session.
- Needs an active Dhan Data API plan; if markets are closed, live ticks simply wait.

## Web Terminal (React/Next.js + FastAPI)

A second frontend in `web/` (App Router, TypeScript, `lightweight-charts`) backed by a
small FastAPI server (`server/`) that reuses the exact same live engines as the Gradio
app: **US Live (Alpaca IEX or Finnhub, yfinance fallback)** and **Dhan NSE live feed**.

Features mirrored from the Gradio UI:

- Three data sources with one click: `US · Alpaca`, `US · Finnhub`, `Dhan · NSE`.
- Symbol combobox (US presets and Dhan presets + custom symbols), timeframe (5m/15m/1h),
  strategy (Price Action / Trend Reversal / Breakout & Breakdown) and EMA/pivot params.
- Candlestick chart with EMA 9/21/169 + VWAP overlay toggles and a live in-progress bar.
- Trend / Signal badge (TREND UPTREND ▲ + SIGNAL BUY), signal panel
  (side, confidence, RVOL, RSI, BOS, FVG, S/R) and AI analysis panel with model votes.
- Watchlist with live US quotes and % change; every row switches the feed.
- Live updates every ~1s over WebSocket (price + live candle); snapshot/candles refresh
  by polling. The web UI previews trades only - it never places orders.

Run it (two terminals):

```powershell
# 1) backend - serves both US and Dhan live feeds
.\.venv\Scripts\python.exe -m uvicorn server.main:app --host 0.0.0.0 --port 8000

# 2) frontend
cd web
npm install
npm run dev       # http://localhost:3000
```

Production build: `npm run build` then `npm start`. The frontend proxies REST calls
same-origin through `/api/proxy`, which forwards to `BACKEND_URL` (default
`http://127.0.0.1:8000`); the WebSocket connects to `NEXT_PUBLIC_WS_URL` (default
`ws://localhost:8000`). See `web/.env.local.example`.

Credentials for all three feeds come from `.env` (see above): Alpaca, Finnhub and
Dhan. The health bar in the UI shows which feeds are configured.
