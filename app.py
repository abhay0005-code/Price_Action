import os

import gradio as gr
import pandas as pd

from strategy import PriceActionStrategy
from trend_reversal import TrendReversalStrategy
from breakout_breakdown import BreakoutBreakdownStrategy
from ai_signal import list_llm_providers, llm_models_for, llm_default_model
from dhan_feed import LiveSignalEngine
from finnhub_feed import FinnhubLiveEngine
from alpaca_feed import AlpacaLiveEngine

engine: LiveSignalEngine | None = None
engine_us: FinnhubLiveEngine | None = None

_ENV_PROVIDER = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
_ENV_MODEL = (os.environ.get("LLM_MODEL") or "").strip()
_PROVIDERS = list_llm_providers()
if _ENV_PROVIDER and _ENV_PROVIDER not in _PROVIDERS:
    _PROVIDERS.append(_ENV_PROVIDER)
_INIT_PROVIDER = _ENV_PROVIDER or "ollama"
_INIT_MODEL = _ENV_MODEL or llm_default_model(_INIT_PROVIDER)

TREND_COLORS = {"UPTREND": "#16a34a", "DOWNTREND": "#dc2626", "SIDEWAYS": "#ca8a04"}
SIGNAL_COLORS = {"BUY": "#16a34a", "SELL": "#dc2626", "HOLD": "#ca8a04", "FORMING": "#ca8a04"}
TREND_ARROWS = {"UPTREND": "&#9650;", "DOWNTREND": "&#9660;", "SIDEWAYS": "&#9658;"}


def color_badge(trend, signal):
    t = (trend or "").upper()
    s = (signal or "").upper()
    tc = TREND_COLORS.get(t, "#ca8a04")
    sc = SIGNAL_COLORS.get(s, "#ca8a04")
    arrow = TREND_ARROWS.get(t, "►")
    return (
        f'<div style="font-family:sans-serif;font-size:22px;line-height:1.6;'
        f'display:flex;gap:18px;align-items:center;flex-wrap:wrap;">'
        f'<span style="font-weight:800;color:{tc}">TREND {t} {arrow}</span>'
        f'<span style="font-weight:800;color:{sc}">SIGNAL {s}</span>'
        f"</div>"
    )


def run_strategy(file, strategy, timeframe, fast, slow, left, right):
    if file is None:
        return "Upload a CSV file.", None, ""
    df = pd.read_csv(file.name if hasattr(file, "name") else file, low_memory=False)
    req = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = req - set(df.columns)
    if missing:
        return "Missing columns: " + ", ".join(sorted(missing)), None, ""
    df["timestamp"] = pd.to_datetime(df.timestamp)
    if "Trend Reversal" in (strategy or ""):
        s = TrendReversalStrategy()
        tf = "5m base / 15m / 1h"
    elif "Breakout" in (strategy or ""):
        s = BreakoutBreakdownStrategy()
        tf = "5m base / 15m / 1h"
    else:
        s = PriceActionStrategy(timeframe, int(fast), int(slow), int(left), int(right))
        tf = s.timeframe
    result = s.generate_signal(df)
    x = s.latest_signal(df)
    if isinstance(s, TrendReversalStrategy):
        sname = "Trend Reversal"
    elif isinstance(s, BreakoutBreakdownStrategy):
        sname = "Breakout / Breakdown"
    else:
        sname = "Price Action"
    summary = (
        f"STRATEGY: {sname}\n"
        f"TIMEFRAMES: {tf}\n"
        f"PRICE: {x['price']:.2f}\n"
        f"TREND: {x['trend']}  (direction / stop direction)\n"
        f"POSITION: {x['position']}\n"
        f"SUPPORT: {x['support']:.2f}\n"
        f"RESISTANCE: {x['resistance']:.2f}\n"
        f"EMA FAST: {x['ema_fast']:.2f}\n"
        f"EMA SLOW: {x['ema_slow']:.2f}\n"
        f"SIGNAL: {x['signal']}\n"
        f"REASON: {x['reason']}"
    )
    cols = [
        "timestamp", "open", "high", "low", "close", "volume",
        "ema_fast", "ema_slow", "support", "resistance", "trend", "signal", "position", "reason",
    ]
    return summary, result[cols].tail(100), color_badge(x["trend"], x["signal"])


def connect_live(symbol, strategy, timeframe, fast, slow, left, right):
    global engine
    try:
        if engine is not None:
            engine.stop()
    except Exception:
        pass
    engine = None
    try:
        engine = LiveSignalEngine(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            fast_ema=int(fast),
            slow_ema=int(slow),
            pivot_left=int(left),
            pivot_right=int(right),
        )
        return engine.start()
    except Exception as exc:
        return f"error: {exc}"


def stop_live():
    global engine
    if engine is not None:
        engine.stop()
        engine = None
    return "stopped"


# --------------------------------------------------------------------- US Market (Finnhub / Alpaca)

US_PRESET_SYMBOLS = ["AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META", "GOOGL", "AMD", "JPM"]

# --------------------------------------------------------------------- Dhan (NSE)

DHAN_PRESET_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK",
    "BHARTIARTL", "ITC", "LT", "AXISBANK", "BAJFINANCE", "HINDUNILVR",
    "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIENT", "MARUTI", "SUNPHARMA", "TITAN",
    "NIFTY 50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX",
]


def connect_us(symbol, provider, strategy, timeframe, fast, slow, left, right):
    global engine_us
    try:
        if engine_us is not None:
            engine_us.stop()
    except Exception:
        pass
    engine_us = None
    cls = AlpacaLiveEngine if (provider or "").lower() == "alpaca" else FinnhubLiveEngine
    try:
        engine_us = cls(
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy,
            fast_ema=int(fast),
            slow_ema=int(slow),
            pivot_left=int(left),
            pivot_right=int(right),
        )
        return engine_us.start()
    except Exception as exc:
        return f"error: {exc}"


def stop_us():
    global engine_us
    if engine_us is not None:
        engine_us.stop()
        engine_us = None
    return "stopped"


def us_refresh(quantity, sl_pct, current=None, llm_provider=None, llm_model=None, force_ai=False):
    global engine_us
    if engine_us is None:
        return (
            "not connected", None, "", "", "",
            gr.update(choices=[], value=None), "", "", "", "", None,
        )
    try:
        summary, df = engine_us.snapshot()
    except Exception as exc:
        return (
            f"error: {exc}", None, str(exc), "", "",
            gr.update(choices=[], value=None), "", "", "", "", None,
        )
    if summary is None:
        return (
            engine_us.status, None, engine_us.error or "", "", "",
            gr.update(choices=[], value=None), "", "", "", "", None,
        )
    text = (
        f"INSTRUMENT: {summary['display']} | {summary['timeframe']}\n"
        f"LIVE PRICE: {summary['live_price']:.2f}\n"
        f"CANDLE: {summary['candle_time']}  (closes {summary['closes_at']})\n"
        f"TREND: {summary['trend']}  (direction / stop direction)\n"
        f"POSITION: {summary['position']}\n"
        f"SUPPORT: {summary['support']:.2f}\n"
        f"RESISTANCE: {summary['resistance']:.2f}\n"
        f"EMA FAST: {summary['ema_fast']:.2f}\n"
        f"EMA SLOW: {summary['ema_slow']:.2f}\n"
        f"SIGNAL (last closed candle): {summary['signal']}\n"
        f"REASON: {summary['reason']}"
    )
    cols = [
        "timestamp", "open", "high", "low", "close", "volume",
        "ema_fast", "ema_slow", "support", "resistance", "trend", "signal", "position", "reason",
    ]
    badge = color_badge(summary["trend"], summary["signal"])
    preview = "preview unavailable"
    try:
        preview = engine_us.trade_preview(int(quantity), float(sl_pct))
    except Exception as exc:
        preview = f"preview error: {exc}"

    ai_info, ai_llm, ai_badge, ai_table = _ai_empty("AI: waiting for closed candles")
    try:
        engine_us.set_llm(provider=llm_provider, model=llm_model)
        ai = engine_us.ai_signal(force=force_ai)
        ai_info, ai_llm, ai_badge, ai_table = _format_ai(ai)
    except Exception as exc:
        ai_info = f"AI error: {exc}"

    return (
        text,
        df[cols].tail(60),
        engine_us.error or "",
        badge,
        preview,
        gr.update(choices=[], value=None),
        "",
        ai_info,
        ai_llm,
        ai_badge,
        ai_table,
    )


def us_place_order(quantity, sl_pct):
    global engine_us
    if engine_us is None:
        return "not connected - start the live feed first"
    try:
        return engine_us.place_signal_order(int(quantity), float(sl_pct))
    except Exception as exc:
        return f"order error: {exc}"


def _ai_empty(note=""):
    return note, "", "", None


def _format_ai(ai):
    status = ai.get("status")
    if status != "ok":
        return _ai_empty(f"AI: {ai.get('note', status)}")
    sig = ai["signal"]
    conf = ai["confidence"]
    badge = (
        f'<div style="font-family:sans-serif;font-size:22px;line-height:1.6;'
        f'display:flex;gap:18px;align-items:center;flex-wrap:wrap;">'
        f'<span style="font-weight:800;color:#2563eb">AI SIGNAL</span>'
        f'<span style="font-weight:800;color:{SIGNAL_COLORS.get(sig, "#ca8a04")}">{sig}</span>'
        f'<span style="color:#475569">confidence {conf:.0%}</span>'
        f'</div>'
    )
    lines = [
        f"TIMESTAMP: {ai['timestamp']}   (re-computed on every 5m candle close)",
        f"PRICE: {float(ai['price']):.2f}",
        f"AI SIGNAL: {sig}   confidence {conf:.0%}   ensemble score {ai['score']:+.2f}",
        f"REASON: {ai['reason']}",
        "",
        "INDICATORS:",
    ]
    ind = ai.get("indicators", {})
    rsi = ind.get("rsi")
    lines.append(
        f"  RSI {rsi:.1f} | EMA169 {ind.get('ema169', float('nan')):.2f}"
        f" | EMA9 {ind.get('ema9', float('nan')):.2f} | EMA21 {ind.get('ema21', float('nan')):.2f}"
        f" | close-vs-169 {ind.get('close_vs_ema169_pct', 0.0):+.2f}% | vol-z {ind.get('vol_z', 0.0):+.2f}"
    )
    lines.append("")
    lines.append("MODEL VOTES:")
    vote_txt = {1: "BUY", -1: "SELL", 0: "HOLD"}
    for name, m in ai.get("models", {}).items():
        if not m.get("active"):
            lines.append(f"  {name.upper():10s} skipped ({m.get('note', 'n/a')})")
            continue
        v = m.get("vote")
        if v == 1:
            vote = "BUY"
        elif v == -1:
            vote = "SELL"
        else:
            vote = "HOLD"
        if name == "arima":
            extra = f"forecast {m.get('forecast', float('nan')):.2f}"
        elif name == "garch":
            extra = (f"mean {m.get('mean_forecast_pct', 0.0):+.3f}% "
                     f"vol {m.get('vol_forecast_pct', 0.0):.3f}%")
        elif name == "kalman":
            extra = (f"slope {m.get('slope', 0.0):+.5f} "
                     f"next {m.get('next_price', float('nan')):.2f}")
        elif name in ("xgboost", "lstm"):
            extra = (f"p_BUY {m.get('p_buy', 0.0):.2f} p_SELL {m.get('p_sell', 0.0):.2f} "
                     f"p_HOLD {m.get('p_hold', 0.0):.2f}")
        else:
            extra = ""
        lines.append(f"  {name.upper():10s} {vote:4s} {extra}")
    lines.append("")
    lines.append("STRATEGY SIGNALS:")
    for name, st in ai.get("strategies", {}).items():
        lines.append(f"  {name:16s} {st.get('signal', 'n/a')}")
    if ai.get("llm_enabled") and ai.get("llm"):
        llm = ai["llm"]
        llm_txt = (
            f"LLM [{llm.get('label', '')}]: {llm.get('signal')} "
            f"(conf {llm.get('confidence', 0.5):.0%}) - {llm.get('reason', '')}"
        )
    elif ai.get("llm"):
        llm = ai["llm"]
        llm_txt = f"LLM [{llm.get('label', '')}]: {llm.get('reason', 'no verdict')}"
    else:
        llm_txt = "LLM: not configured (select a provider/model above, or set LLM_BASE_URL / LLM_MODEL in .env)"
    hist = ai.get("history", [])
    table = (
        pd.DataFrame(hist, columns=["timestamp", "signal", "confidence", "score"])
        if hist else None
    )
    return "\n".join(lines), llm_txt, badge, table


def update_llm_models(provider):
    models = llm_models_for(provider)
    value = models[0] if models else ""
    return gr.update(choices=models, value=value)


def run_ai_now(quantity, sl_pct, current, provider, model):
    return refresh_live(quantity, sl_pct, current, provider, model, force_ai=True)


def refresh_live(quantity, sl_pct, current=None, llm_provider=None, llm_model=None, force_ai=False):
    global engine
    if engine is None:
        return "not connected", None, "", "", "", gr.update(choices=[], value=None), "", "", "", "", None
    try:
        summary, df = engine.snapshot()
    except Exception as exc:
        return (f"error: {exc}", None, str(exc), "", "",
                gr.update(choices=[], value=None), "", "", "", "", None)
    if summary is None:
        return engine.status, None, engine.error or "", "", "", gr.update(choices=[], value=None), "", "", "", "", None
    text = (
        f"INSTRUMENT: {summary['display']} | {summary['timeframe']}\n"
        f"LIVE PRICE: {summary['live_price']:.2f}\n"
        f"CANDLE: {summary['candle_time']}  (closes {summary['closes_at']})\n"
        f"TREND: {summary['trend']}  (direction / stop direction)\n"
        f"POSITION: {summary['position']}\n"
        f"SUPPORT: {summary['support']:.2f}\n"
        f"RESISTANCE: {summary['resistance']:.2f}\n"
        f"EMA FAST: {summary['ema_fast']:.2f}\n"
        f"EMA SLOW: {summary['ema_slow']:.2f}\n"
        f"SIGNAL (last closed candle): {summary['signal']}\n"
        f"REASON: {summary['reason']}"
    )
    cols = [
        "timestamp", "open", "high", "low", "close", "volume",
        "ema_fast", "ema_slow", "support", "resistance", "trend", "signal", "position", "reason",
    ]
    badge = color_badge(summary["trend"], summary["signal"])
    preview = "preview unavailable"
    try:
        preview = engine.trade_preview(int(quantity), float(sl_pct))
    except Exception as exc:
        preview = f"preview error: {exc}"

    opt_choices, opt_info = [], ""
    try:
        if engine.option_segment and summary["signal"] in ("BUY", "SELL"):
            entries, expiry, spot = engine.option_choices()
            opt_choices = [(e["label"], e["security_id"]) for e in entries]
            if entries:
                lot = entries[0]["lot_size"]
                opt_info = (
                    f"Spot {spot:,.0f} | Expiry {expiry} | "
                    f"Lot size {lot} | Side {'CALL (BUY)' if summary['signal'] == 'BUY' else 'PUT (SELL)'}"
                )
    except Exception as exc:
        opt_info = f"option error: {exc}"

    if current in [value for _, value in opt_choices]:
        sel = current
    elif opt_choices:
        sel = opt_choices[0][1]
    else:
        sel = None

    ai_info, ai_llm, ai_badge, ai_table = _ai_empty("AI: waiting for closed candles")
    try:
        engine.set_llm(provider=llm_provider, model=llm_model)
        ai = engine.ai_signal(force=force_ai)
        ai_info, ai_llm, ai_badge, ai_table = _format_ai(ai)
    except Exception as exc:
        ai_info = f"AI error: {exc}"

    return (
        text,
        df[cols].tail(60),
        engine.error or "",
        badge,
        preview,
        (gr.update(choices=opt_choices, value=sel)
         if opt_choices else gr.update(choices=[], value=None)),
        opt_info,
        ai_info,
        ai_llm,
        ai_badge,
        ai_table,
    )


def place_order(quantity, sl_pct):
    global engine
    if engine is None:
        return "not connected - start the live feed first"
    try:
        return engine.place_signal_order(int(quantity), float(sl_pct))
    except Exception as exc:
        return f"order error: {exc}"


def place_option(security_id, lots, sl_pct):
    global engine
    if engine is None:
        return "not connected - start the live feed first"
    if not security_id:
        return "pick a strike first"
    try:
        return engine.place_option_order(str(security_id), int(lots or 1), float(sl_pct))
    except Exception as exc:
        return f"order error: {exc}"


with gr.Blocks(title="Price Action Trading Terminal") as demo:
    gr.Markdown("# 📈 Price Action + Support/Resistance Trading Terminal")
    gr.Markdown("5m / 15m / 1h strategy. Signals are evaluated on completed candles.")

    with gr.Tab("CSV Backtest"):
        with gr.Row():
            file = gr.File(label="OHLCV CSV", file_types=[".csv"])
            strategy = gr.Dropdown(
                ["Price Action", "Trend Reversal", "Breakout / Breakdown"],
                value="Price Action",
                label="Strategy",
            )
            timeframe = gr.Dropdown(["5m", "15m", "1h"], value="5m", label="Timeframe (Price Action)")
        with gr.Row():
            fast = gr.Number(value=20, label="Fast EMA")
            slow = gr.Number(value=50, label="Slow EMA")
            left = gr.Number(value=3, label="Pivot Left")
            right = gr.Number(value=3, label="Pivot Right")
        run = gr.Button("Generate Signal", variant="primary")
        csv_trend = gr.HTML(label="Trend / Signal")
        csv_signal = gr.Textbox(label="Latest Signal", lines=11)
        csv_table = gr.Dataframe(label="Signal History", interactive=False)
        run.click(
            run_strategy,
            [file, strategy, timeframe, fast, slow, left, right],
            [csv_signal, csv_table, csv_trend],
        )


    with gr.Tab("Live US Market Feed (Finnhub / Alpaca)"):
        gr.Markdown(
            "Streams live US market data, builds candles and emits signals.\n"
            "- **Alpaca** (recommended, no candle-403 limits): needs `APCA_API_KEY_ID=` and "
            "`APCA_API_SECRET_KEY=` in `.env` (paper keys work: "
            "https://app.alpaca.markets/paper)\n"
            "- **Finnhub**: needs `FINNHUB_API_KEY=` in `.env` "
            "(https://finnhub.io/register). Free keys cannot seed history from "
            "/stock/candle, falling back to yfinance.\n\n"
            "Paper-trade only - no US broker is integrated."
        )
        with gr.Row():
            us_provider = gr.Dropdown(
                ["alpaca", "finnhub"],
                value="alpaca",
                label="Data Provider",
            )
            us_symbol = gr.Dropdown(
                choices=US_PRESET_SYMBOLS, value="AAPL",
                label="Symbol (US ticker)",
                interactive=True,
            )
            us_strategy = gr.Dropdown(
                ["Price Action", "Trend Reversal", "Breakout / Breakdown"],
                value="Price Action",
                label="Strategy",
            )
            us_tf = gr.Dropdown(["5m", "15m", "1h"], value="5m", label="Timeframe")
        with gr.Row():
            us_fast = gr.Number(value=20, label="Fast EMA")
            us_slow = gr.Number(value=50, label="Slow EMA")
            us_left = gr.Number(value=3, label="Pivot Left")
            us_right = gr.Number(value=3, label="Pivot Right")
        with gr.Row():
            us_connect = gr.Button("Start Live Feed", variant="primary")
            us_stop = gr.Button("Stop")
        us_status = gr.Textbox(label="Status", lines=2)
        us_trend = gr.HTML(label="Trend / Signal")
        us_signal = gr.Textbox(label="Latest Live Signal", lines=10)
        us_table = gr.Dataframe(label="Live Signal History", interactive=False)
        us_err = gr.Textbox(label="Feed Error", lines=1)
        gr.Markdown(
            "### AI Signal (auto on every 5m candle close)\n"
            "169 EMA + ARIMA(1,1,1) + GARCH(1,1) + Kalman + LSTM + XGBoost "
            "(trained on live data) + the 3 rule strategies, voted into one "
            "AI BUY/SELL/HOLD."
        )
        with gr.Row():
            us_llm_provider = gr.Dropdown(
                choices=_PROVIDERS,
                value=_INIT_PROVIDER,
                label="LLM Provider (US)",
                info="Ollama runs locally (no key). Others need their API key: "
                "GROQ_KEY/HF_API_TOKEN/OPENROUTER_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY in .env",
            )
            us_llm_model = gr.Dropdown(
                choices=llm_models_for(_INIT_PROVIDER) or [_INIT_MODEL] or [],
                value=_INIT_MODEL,
                label="LLM Model (US)",
            )
            us_run_ai = gr.Button("Run AI Analysis", variant="primary")
        us_ai_badge = gr.HTML(label="AI Signal Verdict")
        with gr.Row():
            us_ai_info = gr.Textbox(label="AI Analysis Breakdown", lines=13, interactive=False)
            us_ai_llm = gr.Textbox(label="LLM Verdict", lines=5, interactive=False)
        us_ai_table = gr.Dataframe(label="AI Signal History (closed candles)", interactive=False)

        gr.Markdown("### Paper Trade (US market - no live broker)")
        with gr.Row():
            us_qty = gr.Number(value=5, label="Quantity (shares)", precision=0)
            us_sl_pct = gr.Number(value=2.0, label="Stop-Loss % from signal price")
        us_trade_preview = gr.Textbox(label="Paper Trade Preview", lines=10)
        us_order_status = gr.Textbox(label="Order Response", lines=6, interactive=False)
        us_place_btn = gr.Button("Paper Trade (preview + place)", variant="primary")

        us_connect.click(
            connect_us,
            [us_symbol, us_provider, us_strategy, us_tf, us_fast, us_slow, us_left, us_right],
            [us_status],
        )
        us_stop.click(stop_us, outputs=[us_status])
        us_place_btn.click(us_place_order, [us_qty, us_sl_pct], [us_order_status])

        us_timer = gr.Timer(1.0)
        us_opt_dd = gr.Dropdown(choices=[], visible=False, label="Option Strikes (US - unavailable)")
        us_opt_info = gr.Textbox(label="Option Info (US - unavailable)", visible=False)
        us_outputs = [
            us_signal, us_table, us_err, us_trend, us_trade_preview,
            us_opt_dd, us_opt_info, us_ai_info, us_ai_llm, us_ai_badge, us_ai_table,
        ]
        us_inputs = [us_qty, us_sl_pct, us_llm_provider, us_llm_model]
        us_timer.tick(
            us_refresh,
            inputs=us_inputs,
            outputs=us_outputs,
        )
        us_llm_provider.change(update_llm_models, [us_llm_provider], [us_llm_model])
        us_llm_model.change(us_refresh, us_inputs, us_outputs)
        us_run_ai.click(us_refresh, us_inputs, us_outputs)


    with gr.Tab("Live Dhan Feed"):
        gr.Markdown(
            "Streams a DhanHQ WebSocket, builds candles and emits signals. "
            "Needs DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in `.env`."
        )
        with gr.Row():
            symbol = gr.Dropdown(
                choices=DHAN_PRESET_SYMBOLS,
                value="RELIANCE",
                label="Symbol (NSE equity, or NIFTY 50 / BANKNIFTY / SENSEX)",
                interactive=True,
                allow_custom_value=True,
            )
            live_strategy = gr.Dropdown(
                ["Price Action", "Trend Reversal", "Breakout / Breakdown"],
                value="Price Action",
                label="Strategy",
            )
            live_tf = gr.Dropdown(["5m", "15m", "1h"], value="5m", label="Timeframe")
        with gr.Row():
            lfast = gr.Number(value=20, label="Fast EMA")
            lslow = gr.Number(value=50, label="Slow EMA")
            lleft = gr.Number(value=3, label="Pivot Left")
            lright = gr.Number(value=3, label="Pivot Right")
        with gr.Row():
            connect = gr.Button("Start Live Feed", variant="primary")
            stop = gr.Button("Stop")
        status = gr.Textbox(label="Status", lines=2)
        live_trend = gr.HTML(label="Trend / Signal")
        live_signal = gr.Textbox(label="Latest Live Signal", lines=10)
        live_table = gr.Dataframe(label="Live Signal History", interactive=False)
        err = gr.Textbox(label="Feed Error", lines=1)


        gr.Markdown(
            "### AI Signal (auto on every 5m candle close)\n"
            "169 EMA + ARIMA(1,1,1) + GARCH(1,1) + Kalman + LSTM + XGBoost "
            "(trained on live data) + the 3 rule strategies, voted into one "
            "AI BUY/SELL/HOLD."
        )
        with gr.Row():
            llm_provider = gr.Dropdown(
                choices=_PROVIDERS,
                value=_INIT_PROVIDER,
                label="LLM Provider",
                info="Ollama runs locally (no key). Others need their API key: "
                "GROQ_KEY/HF_API_TOKEN/OPENROUTER_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY in .env",
            )
            llm_model = gr.Dropdown(
                choices=llm_models_for(_INIT_PROVIDER) or [_INIT_MODEL] or [],
                value=_INIT_MODEL,
                label="LLM Model",
            )
            run_ai = gr.Button("Run AI Analysis", variant="primary")
        ai_badge = gr.HTML(label="AI Signal Verdict")
        with gr.Row():
            ai_info = gr.Textbox(label="AI Analysis Breakdown", lines=13, interactive=False)
            ai_llm = gr.Textbox(label="LLM Verdict", lines=5, interactive=False)
        ai_table = gr.Dataframe(label="AI Signal History (closed candles)", interactive=False)

        gr.Markdown("### Manual order to Dhan (INTRADAY, entry LIMIT + stop-loss)")
        with gr.Row():
            order_qty = gr.Number(value=5, label="Order Quantity", precision=0)
            sl_pct = gr.Number(value=2.0, label="Stop-Loss % from signal price")
        trade_preview = gr.Textbox(label="Order Preview (updates each signal)", lines=10)
        with gr.Row():
            place_btn = gr.Button("Place Order on Dhan", variant="primary")
            order_status = gr.Textbox(label="Order Response", lines=6, interactive=False)

        gr.Markdown(
            "### Index Options (NIFTY / BANKNIFTY / SENSEX)\n"
            "On a BUY signal 3 Call strikes are shown; on a SELL signal 3 Put strikes. "
            "Buy the option at its live premium with a stop-loss on the premium."
        )
        opt_info = gr.Textbox(label="Option Info", lines=1, interactive=False)
        opt_dd = gr.Dropdown(choices=[], label="Select Option Strike")
        with gr.Row():
            opt_lots = gr.Number(value=1, label="Lots", precision=0)
            opt_sl = gr.Number(value=2.0, label="Stop-Loss % on premium")
            opt_place = gr.Button("Buy Selected Option", variant="primary")
        opt_status = gr.Textbox(label="Option Order Response", lines=12, interactive=False)

        connect.click(
            connect_live,
            [symbol, live_strategy, live_tf, lfast, lslow, lleft, lright],
            [status],
        )
        stop.click(stop_live, outputs=[status])
        place_btn.click(place_order, [order_qty, sl_pct], [order_status])
        opt_place.click(place_option, [opt_dd, opt_lots, opt_sl], [opt_status])

        timer = gr.Timer(1.0)
        llm_outputs = [
            live_signal, live_table, err, live_trend, trade_preview,
            opt_dd, opt_info, ai_info, ai_llm, ai_badge, ai_table,
        ]
        llm_inputs = [order_qty, sl_pct, opt_dd, llm_provider, llm_model]
        timer.tick(
            refresh_live,
            inputs=llm_inputs,
            outputs=llm_outputs,
        )
        llm_provider.change(update_llm_models, [llm_provider], [llm_model])
        llm_model.change(run_ai_now, llm_inputs, llm_outputs)
        run_ai.click(run_ai_now, llm_inputs, llm_outputs)


if __name__ == "__main__":
    demo.launch()

