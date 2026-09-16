# Price Action Trading Terminal — Application Flow Diagram

## 1. High-Level Architecture

```mermaid
graph TB
    subgraph "🎨 Frontend"
        GRADIO["Gradio UI<br/>(app.py)"]
        NEXTJS["Next.js UI<br/>(web/)"]
    end

    subgraph "🌐 Backend"
        FASTAPI["FastAPI Server<br/>(server/main.py)"]
        MANAGER["TerminalManager<br/>(server/engine.py)"]
    end

    subgraph "📊 Live Engines"
        ALPACA["AlpacaLiveEngine"]
        FINNHUB["FinnhubLiveEngine"]
        ALPHA_VANTAGE["AlphaVantageLiveEngine"]
        DHAN["LiveSignalEngine<br/>(Dhan NSE)"]
        DELTA["DeltaIndiaLiveEngine"]
    end

    subgraph "🧠 Strategy Layer"
        PA_STRAT["PriceActionStrategy<br/>(strategy.py)"]
        TR_STRAT["TrendReversalStrategy<br/>(trend_reversal.py)"]
        BB_STRAT["BreakoutBreakdownStrategy<br/>(breakout_breakdown.py)"]
    end

    subgraph "🤖 AI Layer"
        AI_ENGINE["AISignalEngine<br/>(ai_signal.py)"]
        LLM_JUDGE["LLMJudge<br/>(OpenAI-compatible)"]
        ML_MODELS["ARIMA / GARCH / Kalman / XGBoost"]
    end

    subgraph "🔌 External APIs"
        ALPACA_API["Alpaca Market Data"]
        FINNHUB_API["Finnhub WebSocket"]
        AV_API["Alpha Vantage"]
        DHAN_API["DhanHQ API"]
        DELTA_API["Delta Exchange"]
        JESSE_API["Jesse Server<br/>(localhost:9000)"]
    end

    GRADIO -->|"connect_live()<br/>connect_us()"| MANAGER
    NEXTJS -->|"HTTP API<br/>/api/select"| FASTAPI
    FASTAPI --> MANAGER
    MANAGER --> ALPACA & FINNHUB & ALPHA_VANTAGE & DHAN & DELTA
    ALPACA --> ALPACA_API
    FINNHUB --> FINNHUB_API
    ALPHA_VANTAGE --> AV_API
    DHAN --> DHAN_API
    DELTA --> DELTA_API
    ALPACA & FINNHUB & ALPHA_VANTAGE & DHAN & DELTA --> PA_STRAT & TR_STRAT & BB_STRAT
    PA_STRAT & TR_STRAT & BB_STRAT --> AI_ENGINE
    AI_ENGINE --> ML_MODELS & LLM_JUDGE
    FASTAPI -.->|"HTTP GET"| JESSE_API
```

---

## 2. Application Startup Flow

```mermaid
sequenceDiagram
    participant User
    participant Entry as Entry Point
    participant Env as .env File
    participant Engine as Live Engine
    participant WS as WebSocket

    Note over Entry: == Path A: Gradio (app.py) ==
    Entry->>Env: Load credentials (Dhan, Finnhub, Alpaca, etc.)
    Entry->>Entry: Import strategy classes + LLM providers
    Entry->>User: Render Gradio UI (3 tabs: India/Dhan, US, Backtest)

    Note over Entry: == Path B: FastAPI + Next.js ==
    Entry->>Entry: uvicorn server.main:app --port 8000
    Entry->>Env: Load .env credentials
    Entry->>Entry: Create TerminalManager singleton
    Entry->>Entry: Mount static Next.js build (web/out/)
    Entry->>User: Serve frontend at http://localhost:8000
```

---

## 3. Symbol Selection & Engine Startup Flow

```mermaid
sequenceDiagram
    participant UI as Frontend
    participant API as FastAPI /api/select
    participant MGR as TerminalManager
    participant ENGINE as Live Engine
    participant FEED as Market Feed API
    participant STRAT as Strategy

    UI->>API: POST /api/select {symbol, market, provider, timeframe, strategy}
    API->>MGR: select() / select_dhan() / select_delta()

    alt market == "us"
        MGR->>MGR: Stop existing engine if running
        MGR->>ENGINE: Create AlpacaLiveEngine / FinnhubLiveEngine / AlphaVantageLiveEngine
    else market == "dhan"
        MGR->>ENGINE: Create LiveSignalEngine (dhan_feed.py)
    else market == "delta"
        MGR->>ENGINE: Create DeltaIndiaLiveEngine
    end

    ENGINE->>ENGINE: resolve_symbol(symbol)
    ENGINE->>ENGINE: Validate credentials
    ENGINE->>ENGINE: Instantiate Strategy (PriceAction / TrendReversal / BreakoutBreakdown)

    ENGINE->>FEED: _seed_finnhub() — Fetch historical candles (REST)
    FEED-->>ENGINE: Return seed candles (5m/15m/1h)
    ENGINE->>ENGINE: Build DataFrame, compute indicators

    ENGINE->>ENGINE: Start background thread (_thread)
    ENGINE->>FEED: Connect WebSocket (live tick stream)
    FEED-->>ENGINE: Stream ticks in real-time

    MGR-->>API: Return {ok, symbol, status, engine brief}
    API-->>UI: JSON response
```

---

## 4. Live Data Stream & Candle Build Flow

```mermaid
sequenceDiagram
    participant FEED as Market WebSocket
    participant ENGINE as Live Engine
    participant CANDLE as Candle Builder
    participant STRAT as Strategy
    participant AI as AI Engine
    participant UI as Frontend

    loop Every tick from WebSocket
        FEED->>ENGINE: on_message(tick data)
        ENGINE->>ENGINE: Parse price/volume from tick

        ENGINE->>CANDLE: Update current candle
        Note over CANDLE: Aggregate ticks into OHLCV candle<br/>based on timeframe (5m/15m/1h)

        alt Candle closed (new period starts)
            CANDLE->>ENGINE: Candle closed event
            ENGINE->>ENGINE: Append closed candle to history DataFrame
            ENGINE->>ENGINE: Start new candle

            ENGINE->>STRAT: strategy.generate_signal(history)
            STRAT->>STRAT: calculate_indicators(df)
            Note over STRAT: EMA fast/slow<br/>Pivot highs/lows<br/>Support/Resistance<br/>Trend detection<br/>BUY/SELL/HOLD signal
            STRAT-->>ENGINE: Signal result (trend, signal, position, reason)

            ENGINE->>AI: ai_signal(force=False)
            Note over AI: Run statistical models<br/>ARIMA, GARCH, Kalman, XGBoost<br/>+ Strategy consensus<br/>+ LLM judge (if configured)
            AI-->>ENGINE: AI verdict (BUY/SELL/HOLD + confidence)

            ENGINE->>ENGINE: snapshot() — build live summary
        end

        ENGINE->>UI: Update via WebSocket / Gradio Timer
    end
```

---

## 5. WebSocket Real-Time Push Flow

```mermaid
sequenceDiagram
    participant CLIENT as Next.js Client
    participant WS as /ws WebSocket
    participant MGR as TerminalManager
    participant ENGINE as Live Engine
    participant STRAT as Strategy

    CLIENT->>WS: WebSocket connect
    loop Every 1 second
        WS->>MGR: manager.ws_payload()
        MGR->>ENGINE: engine.snapshot()
        ENGINE->>ENGINE: Get live candle OHLCV
        ENGINE->>STRAT: strategy.generate_signal(history)
        STRAT-->>ENGINE: trend + signal + position
        ENGINE-->>MGR: summary (live_price, candle_time, signal)
        MGR-->>WS: JSON payload

        Note over WS,CLIENT: Payload contains:<br/>- symbol, market, provider<br/>- price, candle_time, closes_at<br/>- live_candle (OHLCV)<br/>- signal {side, strategy, trend, reason}

        WS-->>CLIENT: Send JSON update
    end
```

---

## 6. AI Signal Generation Flow

```mermaid
flowchart TD
    A["ai_signal(force=False)"] --> B{"History available?<br/>Min 30 candles?"}
    B -- No --> C["Return status: not_ready"]
    B -- Yes --> D["build_features(df)"]

    D --> E["Technical Indicators"]
    E --> E1["169 EMA slope & distance"]
    E --> E2["9/21/50 EMA"]
    E --> E3["RSI (14)"]
    E --> E4["Volatility (20-bar std)"]
    E --> E5["Volume z-score"]
    E --> E6["Candle body/range ratios"]
    E --> E7["Range breakout flags"]

    D --> F["Statistical Models"]
    F --> F1["ARIMA(1,1,1) — price direction forecast"]
    F --> F2["GARCH(1,1) — volatility regime"]
    F --> F3["Kalman filter — trend slope"]

    D --> G{"XGBoost available?"}
    G -- Yes --> G1["XGBoost classifier<br/>(trained on live data)"]
    G -- No --> G2["Skip (lightweight mode)"]

    D --> H["Rule Strategies"]
    H --> H1["PriceActionStrategy"]
    H --> H2["TrendReversalStrategy"]
    H --> H3["BreakoutBreakdownStrategy"]

    E1 & E2 & E3 & E4 & E5 & E6 & E7 --> I["Weighted Ensemble Vote"]
    F1 & F2 & F3 --> I
    G1 --> I
    H1 & H2 & H3 --> I

    I --> J{"LLM configured?"}
    J -- Yes --> K["LLMJudge.judge()<br/>OpenAI-compatible API"]
    K --> L["LLM verdict<br/>(BUY/SELL/HOLD + reason)"]
    L --> M["Merge LLM into final signal"]
    J -- No --> N["Use ensemble only"]

    M --> O["Return result:<br/>signal, confidence, score,<br/>reason, models breakdown"]
    N --> O
```

---

## 7. Dhan (India Market) Specific Flow

```mermaid
sequenceDiagram
    participant UI as UI
    participant DHAN as LiveSignalEngine
    participant REST as Dhan REST API
    participant WS as Dhan MarketFeed WS
    participant SECURITY as Security Master CSV

    UI->>DHAN: connect_live(symbol="RELIANCE")
    DHAN->>SECURITY: _security_master() — Load security list
    SECURITY-->>DHAN: Match symbol → security_id, feed_segment

    DHAN->>REST: Fetch seed candles (intraday)
    REST-->>DHAN: Historical OHLCV candles

    DHAN->>WS: MarketFeed subscribe(security_id, feed_segment)
    loop Live ticks
        WS->>DHAN: Quote tick (LTP, volume, bid/ask)
        DHAN->>DHAN: Aggregate into timeframe candle
        DHAN->>DHAN: On candle close → generate_signal()
        DHAN->>DHAN: Run AI analysis
    end

    opt User clicks "Place Order"
        UI->>DHAN: place_order_request()
        DHAN->>REST: dhanhq.place_order()
        REST-->>DHAN: Order confirmation
    end

    opt User clicks "Option Chain"
        UI->>DHAN: fetch_option_chain()
        DHAN->>REST: dhanhq.get_option_chain()
        REST-->>DHAN: Option chain data with Greeks
    end
```

---

## 8. Complete REST API Endpoint Map

```mermaid
flowchart LR
    subgraph "FastAPI Endpoints (server/main.py)"
        direction TB
        H["GET /api/health"]
        S["GET /api/symbols?market="]
        W["GET /api/watchlist"]
        WA["POST /api/watchlist"]
        WD["DELETE /api/watchlist"]
        SEL["POST /api/select"]
        SS["POST /api/stop"]
        SN["GET /api/engine/snapshot"]
        CA["GET /api/engine/candles"]
        AI["GET /api/engine/ai"]
        OP["GET /api/engine/order-preview"]
        LP["GET /api/llm/providers"]
        LS["POST /api/llm/select"]
        LC["POST /api/llm/check"]
        JL["GET /api/strategies/jesse/list"]
        JP["POST /api/strategies/jesse/pull"]
        WS["WS /ws"]
    end

    H -->|"Returns"| H1["status, configured,<br/>markets, active engine"]
    S -->|"Returns"| S1["preset symbols list"]
    W -->|"Returns"| W1["custom watchlist symbols"]
    SEL -->|"Routes to"| SEL1["manager.select()<br/>manager.select_dhan()<br/>manager.select_delta()"]
    SN -->|"Returns"| SN1["live price, trend,<br/>signal, support/resistance,<br/>RSI, VWAP, BOS, FVG"]
    CA -->|"Returns"| CA1["OHLCV bars +<br/>EMA, RSI, signals"]
    AI -->|"Returns"| AI1["signal, confidence,<br/>score, reason,<br/>model breakdown, LLM"]
    OP -->|"Returns"| OP1["paper trade preview"]
    WS -->|"Pushes every 1s"| WS1["live candle,<br/>price, signal"]
```

---

## 9. Gradio UI Tab Flow (app.py)

```mermaid
flowchart TB
    subgraph "Tab 1: India / Dhan"
        I1["User selects symbol<br/>(RELIANCE, NIFTY 50, etc.)"]
        I2["User clicks Connect"]
        I3["connect_live() → LiveSignalEngine"]
        I4["Engine streams Dhan ticks"]
        I5["Timer polls snapshot every 1s"]
        I6["Display: Trend, Signal,<br/>Price, Support/Resistance"]
        I7["Place Order / Option Chain<br/>(Dhan broker integration)"]
    end

    subgraph "Tab 2: US Market"
        U1["User selects symbol<br/>(AAPL, TSLA, NVDA, etc.)"]
        U2["User selects provider<br/>(Alpaca / Finnhub / AlphaVantage)"]
        U3["connect_us() → AlpacaLiveEngine<br/>/ FinnhubLiveEngine"]
        U4["Engine streams US ticks"]
        U5["Timer polls snapshot every 1s"]
        U6["Display: Trend, Signal,<br/>Price, Indicators"]
    end

    subgraph "Tab 3: Backtest CSV"
        B1["User uploads CSV file"]
        B2["User selects strategy"]
        B3["run_strategy() → strategy.generate_signal()"]
        B4["Display: Summary, DataFrame,<br/>Trend/Signal badge"]
    end

    I1 --> I2 --> I3 --> I4 --> I5 --> I6 --> I7
    U1 --> U2 --> U3 --> U4 --> U5 --> U6
    B1 --> B2 --> B3 --> B4
```

---

## 10. Module Dependency Map

```mermaid
graph LR
    subgraph "Entry Points"
        APP["app.py<br/>(Gradio)"]
        MAIN["server/main.py<br/>(FastAPI)"]
    end

    subgraph "Core Engine"
        ENG["server/engine.py<br/>(TerminalManager)"]
        IND["server/indicators.py<br/>(Technical indicators)"]
    end

    subgraph "Feed Engines"
        ALP["alpaca_feed.py"]
        FIN["finnhub_feed.py"]
        AV["alphavantage_feed.py"]
        DHF["dhan_feed.py"]
        DEL["delta_feed.py"]
    end

    subgraph "Strategies"
        STRAT["strategy.py<br/>(PriceAction)"]
        TREND["trend_reversal.py<br/>(TrendReversal)"]
        BREAK["breakout_breakdown.py<br/>(BreakoutBreakdown)"]
    end

    subgraph "AI / ML"
        AISIG["ai_signal.py<br/>(AISignalEngine + LLMJudge)"]
    end

    subgraph "External Dependencies"
        DHANHQ["dhanhq SDK"]
        ALPACA_SDK["alpaca-py SDK"]
        WS_LIB["websocket-client"]
        STATSMODELS["statsmodels<br/>(ARIMA)"]
        ARCH["arch<br/>(GARCH)"]
        XGB["xgboost"]
        TORCH["PyTorch<br/>(LSTM)"]
    end

    APP --> ENG
    MAIN --> ENG
    ENG --> ALP & FIN & AV & DHF & DEL
    ENG --> IND

    ALP --> FIN
    FIN --> STRAT & TREND & BREAK & AISIG
    DHF --> STRAT & TREND & BREAK & AISIG
    DEL --> STRAT & TREND & BREAK & AISIG

    AISIG --> STRAT & TREND & BREAK
    AISIG --> STATSMODELS & ARCH & XGB & TORCH

    ALP --> ALPACA_SDK
    FIN --> WS_LIB
    DHF --> DHANHQ
```

---

## 11. Complete Request Lifecycle

```mermaid
flowchart TD
    START(["User opens<br/>http://localhost:8000"])

    START --> CHECK{"Backend running?"}
    CHECK -- "No" --> START_UVICON["uvicorn server.main:app<br/>--port 8000"]
    CHECK -- "Yes" --> LOAD_ENV["Load .env credentials"]

    START_UVICON --> LOAD_ENV
    LOAD_ENV --> INIT_MGR["Create TerminalManager<br/>singleton (manager.py:909)"]
    INIT_MGR --> SERVE["Serve Next.js build<br/>from web/out/"]
    SERVE --> UI_READY["Frontend loaded in browser"]

    UI_READY --> SELECT_SYMBOL["User selects symbol<br/>+ market + provider"]
    SELECT_SYMBOL --> POST_SELECT["POST /api/select"]
    POST_SELECT --> MANAGER_ROUTE{"Market type?"}

    MANAGER_ROUTE -- "US" --> US_ENG["manager.select()<br/>→ AlpacaLiveEngine/<br/>FinnhubLiveEngine/<br/>AlphaVantageLiveEngine"]
    MANAGER_ROUTE -- "India" --> DHAN_ENG["manager.select_dhan()<br/>→ LiveSignalEngine"]
    MANAGER_ROUTE -- "Delta" --> DELTA_ENG["manager.select_delta()<br/>→ DeltaIndiaLiveEngine"]

    US_ENG --> SEED["Seed with historical candles<br/>(REST API)"]
    DHAN_ENG --> SEED
    DELTA_ENG --> SEED

    SEED --> START_WS["Start background thread<br/>Connect WebSocket feed"]
    START_WS --> LIVE_LOOP["Live tick processing loop"]

    LIVE_LOOP --> AGGREGATE["Aggregate ticks →<br/>OHLCV candle (5m/15m/1h)"]

    AGGREGATE --> CANDLE_CLOSED{"Candle closed?"}
    CANDLE_CLOSED -- "No" --> LIVE_LOOP
    CANDLE_CLOSED -- "Yes" --> SIGNAL["strategy.generate_signal()<br/>→ BUY / SELL / HOLD"]

    SIGNAL --> AI["AISignalEngine.ai_signal()<br/>→ ARIMA + GARCH + Kalman<br/>+ XGBoost + Strategies<br/>+ LLM Judge (optional)"]

    AI --> PUSH_WS["Push update via<br/>WebSocket /ws (every 1s)"]
    PUSH_WS --> RENDER["Frontend renders:<br/>• Live candle chart<br/>• Trend badge<br/>• Signal panel<br/>• AI analysis panel<br/>• Watchlist"]

    RENDER --> LIVE_LOOP
```

---

## 12. Signal Decision Tree

```mermaid
flowchart TD
    A["New closed candle"] --> B["Price Action Rules"]
    B --> B1{"Close > Previous High?"}
    B1 -- "Yes" --> B2["BUY signal"]
    B1 -- "No" --> B3{"Close < Previous Low?"}
    B3 -- "Yes" --> B4["SELL signal"]
    B3 -- "No" --> B5["HOLD"]

    B2 --> C["Position tracking:<br/>FLAT→LONG / SHORT→LONG (reverse)"]
    B4 --> D["Position tracking:<br/>FLAT→SHORT / LONG→SHORT (reverse)"]
    B5 --> E["No position change"]

    C --> F["AI Ensemble Vote"]
    D --> F
    E --> F

    F --> G["Statistical models vote<br/>(ARIMA, GARCH, Kalman)"]
    G --> H["ML model vote<br/>(XGBoost if available)"]
    H --> I["Strategy consensus<br/>(PriceAction + TrendReversal + Breakout)"]

    I --> J{"LLM enabled?"}
    J -- "Yes" --> K["Send context to LLM<br/>→ LLM verdict"]
    J -- "No" --> L["Weighted majority vote"]
    K --> L

    L --> M{"Final signal"}
    M -->|"Score > threshold"| N["BUY with confidence %"]
    M -->|"Score < -threshold"| O["SELL with confidence %"]
    M -->|"In between"| P["HOLD"]
```
