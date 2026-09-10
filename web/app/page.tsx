"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type {
  AiInfo,
  Bar,
  Health,
  Snapshot,
  WatchRow,
  WsUpdate,
  SelectOptions,
} from "@/lib/api";
import {
  aiAnalysis,
  addWatchlist,
  candles,
  health,
  inferError,
  llmCheck,
  llmCatalog as fetchLlmCatalog,
  llmSelect,
  select as apiSelect,
  snapshot,
  symbols,
  watchlist,
  removeWatchlist,
  wsUrl,
} from "@/lib/api";
import type { LlmCatalog } from "@/lib/api";
import Header from "@/components/Header";
import Toolbar, { type Market, type Provider } from "@/components/Toolbar";
import Watchlist from "@/components/Watchlist";
import Chart, { OverlayChips, type OverlayKey } from "@/components/Chart";
import SignalPanel from "@/components/SignalPanel";
import SignalTable from "@/components/SignalTable";
import AIAnalysis from "@/components/AIAnalysis";
import TrendBadge from "@/components/TrendBadge";

const DEFAULT_SYMBOL: Record<Market, string> = { us: "AAPL", dhan: "RELIANCE" };
const WATCHLIST_INTERVAL = 5000;
const SNAPSHOT_INTERVAL = 3000;
const CANDLES_INTERVAL = 30000;

type WsReady = { instance: WebSocket | null; closed: boolean };

export default function Terminal() {
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [healthData, setHealthData] = useState<Health | null>(null);
  const [marketOpen, setMarketOpen] = useState<boolean | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const wsRef = useRef<WsReady>({ instance: null, closed: false });

  const [market, setMarket] = useState<Market>("us");
  const [provider, setProvider] = useState<Provider>("alpaca");
  const [timeframe, setTimeframe] = useState("5m");
  const [strategy, setStrategy] = useState("price_action");
  const [fastEma, setFastEma] = useState(20);
  const [slowEma, setSlowEma] = useState(50);
  const [pivotLeft, setPivotLeft] = useState(3);
  const [pivotRight, setPivotRight] = useState(3);

  const [symbolOptions, setSymbolOptions] = useState<string[]>([]);
  const [activeSymbol, setActiveSymbol] = useState<string>(DEFAULT_SYMBOL.us);
  const [selecting, setSelecting] = useState(false);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [bars, setBars] = useState<Bar[]>([]);
  const [candleError, setCandleError] = useState<string | null>(null);
  const [live, setLive] = useState<WsUpdate["live_candle"]>(null);

  const [rows, setRows] = useState<WatchRow[]>([]);
  const [watchError, setWatchError] = useState<string | null>(null);

  const [connectKey, setConnectKey] = useState(0);
  const lastApplyRef = useRef(`${market}:${provider}:${activeSymbol}`);

  const [overlays, setOverlays] = useState<Record<OverlayKey, boolean>>({
    ema9: true,
    ema21: true,
    ema169: false,
    vwap: true,
  });

  const [ai, setAi] = useState<AiInfo | null>(null);
  const [aiLoading, setAiLoading] = useState(false);

  const [llmCatalog, setLlmCatalog] = useState<LlmCatalog | null>(null);
  const [llmProvider, setLlmProvider] = useState("ollama");
  const [llmModel, setLlmModel] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const llmApiKeyRef = useRef("");

  // eager refs for the WS handler
  const activeRef = useRef(activeSymbol);
  activeRef.current = activeSymbol;
  const marketRef = useRef(market);
  marketRef.current = market;
  const providerRef = useRef(provider);
  providerRef.current = provider;

  // ------------------------------------------------------------- health
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const h = await health();
        if (cancelled) return;
        setHealthData(h);
        setBackendUp(true);
        setMarketOpen(h.market_open?.open ?? null);
        setProvider((p) => p || "alpaca");
      } catch {
        if (!cancelled) setBackendUp(false);
      }
    };
    tick();
    const id = setInterval(tick, WATCHLIST_INTERVAL * 3);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // symbol presets follow the market
  useEffect(() => {
    let cancelled = false;
    symbols(market).then((s) => {
      if (cancelled) return;
      setSymbolOptions(s.symbols);
      setActiveSymbol((cur) => {
        if (s.symbols.includes(cur)) {
          if (lastApplyRef.current !== `${market}:${provider}:${cur}`) requestConnect();
          return cur;
        }
        const def = DEFAULT_SYMBOL[market];
        if (lastApplyRef.current !== `${market}:${provider}:${def}`) requestConnect();
        return def;
      });
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);

  // single place that re-selects the engine when market/provider/symbol changed
  const requestConnect = useCallback(() => setConnectKey((k) => k + 1), []);
  useEffect(() => {
    if (!backendUp) return;
    if (market === "us" && !healthData?.markets?.us?.configured) return;
    if (market === "dhan" && !healthData?.markets?.dhan?.configured) return;
    applySelection(activeSymbol);
    lastApplyRef.current = `${market}:${provider}:${activeSymbol}`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectKey, backendUp]);

  // ------------------------------------------------------------- watchlist
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const wl = await watchlist(market);
        if (cancelled) return;
        setRows(wl.rows);
        setWatchError(wl.error ?? null);
      } catch (e) {
        if (!cancelled) setWatchError(await inferError(e));
      }
    };
    tick();
    const id = setInterval(tick, WATCHLIST_INTERVAL);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [backendUp, market]);

  // ------------------------------------------------------------- websocket
  const connectWs = useCallback(() => {
    if (wsRef.current.closed || wsRef.current.instance?.readyState === WebSocket.OPEN) return;
    try {
      const ws = new WebSocket(wsUrl());
      wsRef.current.instance = ws;
      ws.onopen = () => {
        setWsConnected(true);
        ws.send(JSON.stringify({ symbol: activeRef.current }));
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as WsUpdate;
          if (msg.type !== "update") return;
          if (msg.symbol && msg.symbol !== activeRef.current) return;
          setLive(msg.live_candle ?? null);
        } catch {
          /* malformed frame - ignore */
        }
      };
      ws.onclose = () => {
        setWsConnected(false);
        setTimeout(connectWs, 3000);
      };
      ws.onerror = () => ws.close();
    } catch {
      setTimeout(connectWs, 3000);
    }
  }, []);

  useEffect(() => {
    connectWs();
    return () => {
      wsRef.current.closed = true;
      wsRef.current.instance?.close();
    };
  }, [connectWs]);

  // ------------------------------------------------------------- engine wiring
  const fetchDepth = useCallback(async (symbol: string) => {
    setCandleError(null);
    try {
      const [c, s] = await Promise.all([candles(symbol), snapshot(symbol)]);
      setBars(c.bars);
      setSnap(s);
    } catch (e) {
      setCandleError(await inferError(e));
    }
  }, []);

  const applySelection = useCallback(
    async (symbol: string, opts?: SelectOptions) => {
      setSelecting(true);
      try {
        const merged: SelectOptions = {
          market,
          provider: market === "us" ? provider : "",
          timeframe,
          strategy,
          fastEma,
          slowEma,
          pivotLeft,
          pivotRight,
          ...opts,
        };
        await apiSelect(symbol, merged);
        setActiveSymbol(symbol);
        await fetchDepth(symbol);
        setCandleError(null);
      } catch (e) {
        setCandleError(await inferError(e));
      } finally {
        setSelecting(false);
      }
    },
    [market, provider, timeframe, strategy, fastEma, slowEma, pivotLeft, pivotRight, fetchDepth, requestConnect],
  );

  // periodic refresh
  useEffect(() => {
    if (!backendUp || !activeSymbol) return;
    const id = setInterval(() => {
      snapshot(activeSymbol).then(setSnap).catch(() => {});
    }, SNAPSHOT_INTERVAL);
    return () => clearInterval(id);
  }, [backendUp, activeSymbol]);

  useEffect(() => {
    if (!backendUp || !activeSymbol) return;
    const id = setInterval(() => {
      candles(activeSymbol)
        .then((c) => setBars(c.bars))
        .catch(() => {});
    }, CANDLES_INTERVAL);
    return () => clearInterval(id);
  }, [backendUp, activeSymbol]);

  // ------------------------------------------------------------- AI
  // Load the LLM provider/model catalog and apply the default on startup.
  useEffect(() => {
    let cancelled = false;
    fetchLlmCatalog()
      .then((c) => {
        if (cancelled) return;
        setLlmCatalog(c);
        const defP = c.default_provider || "ollama";
        setLlmProvider(defP);
        const defaultModels = c.models?.[defP] ?? [];
        setLlmModel((cur) => {
          const def = c.default_model || defaultModels[0] || "";
          return cur && defaultModels.includes(cur) ? cur : def;
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshAi = useCallback(
    async (refresh = false, checkConnection = false) => {
      if (!activeSymbol) return;
      setAiLoading(true);
      try {
        if (llmProvider && llmModel) {
          if (checkConnection) {
            await llmCheck(llmProvider, llmModel, llmApiKeyRef.current);
          }
          await llmSelect(llmProvider, llmModel, llmApiKeyRef.current);
        }
        setAi(await aiAnalysis(activeSymbol, refresh));
      } catch (e) {
        setAi({ error: await inferError(e) });
      } finally {
        setAiLoading(false);
      }
    },
    [activeSymbol, llmProvider, llmModel],
  );

  useEffect(() => {
    if (backendUp && activeSymbol) refreshAi(false);
  }, [backendUp, activeSymbol, refreshAi]);

  const onLlmProviderChange = (p: string) => {
    setLlmProvider(p);
    const modelsFor = llmCatalog?.models?.[p] ?? [];
    setLlmModel(modelsFor[0] ?? "");
  };

  const onLlmModelChange = (m: string) => {
    setLlmModel(m);
  };

  const onLlmApiKeyChange = (key: string) => {
    llmApiKeyRef.current = key;
    setLlmApiKey(key);
  };

  // auto-run AI on every closed-candle rollover (mirrors the Gradio timer tick)
  const lastAiCandle = useRef<string>("");
  useEffect(() => {
    const candle = snap?.candle_time;
    if (!candle || candle === lastAiCandle.current) return;
    lastAiCandle.current = candle;
    if (backendUp) refreshAi(false);
  }, [snap?.candle_time, backendUp, refreshAi]);

  // ------------------------------------------------------------- actions
  const onSelectFromWatchlist = (symbol: string) => {
    setActiveSymbol(symbol);
    requestConnect();
  };

  const refreshWatchlist = async (result: Awaited<ReturnType<typeof watchlist>>) => {
    setRows(result.rows);
    setWatchError(result.error ?? null);
  };

  const onAddToWatchlist = async (symbol: string) => {
    try {
      await refreshWatchlist(await addWatchlist(symbol, market));
    } catch (e) {
      setWatchError(await inferError(e));
    }
  };

  const onRemoveFromWatchlist = async (symbol: string) => {
    try {
      await refreshWatchlist(await removeWatchlist(symbol, market));
    } catch (e) {
      setWatchError(await inferError(e));
    }
  };

  const onMarketChange = (m: Market, p: Provider) => {
    if (m === market && (m !== "us" || p === provider)) return;
    setProvider(p);
    setMarket(m);
    requestConnect();
  };

  const onSettingsChange = (s: {
    timeframe?: string;
    strategy?: string;
    fastEma?: number;
    slowEma?: number;
    pivotLeft?: number;
    pivotRight?: number;
  }) => {
    if (s.timeframe) setTimeframe(s.timeframe);
    if (s.strategy) setStrategy(s.strategy);
    if (s.fastEma) setFastEma(s.fastEma);
    if (s.slowEma) setSlowEma(s.slowEma);
    if (s.pivotLeft) setPivotLeft(s.pivotLeft);
    if (s.pivotRight) setPivotRight(s.pivotRight);
  };

  const onConnect = () => applySelection(activeSymbol);
  const onStop = async () => {
    try {
      const { stop } = await import("@/lib/api");
      await stop();
      setSnap(null);
      setBars([]);
    } catch (e) {
      setCandleError(await inferError(e));
    }
  };

  const price = snap?.price ?? live?.close ?? null;
  const connected = snap?.status?.startsWith("LIVE") ?? false;

  return (
    <div className="terminal">
      <Header marketOpen={marketOpen} wsConnected={wsConnected} backendUp={backendUp} />

      {backendUp === false && (
        <div className="banner error">
          Backend unreachable. Start it with: uvicorn server.main:app --host 0.0.0.0 --port 8000
        </div>
      )}
      {candleError && <div className="banner warn">{candleError}</div>}

      <Toolbar
        health={healthData}
        market={market}
        provider={provider}
        timeframe={timeframe}
        strategy={strategy}
        fastEma={fastEma}
        slowEma={slowEma}
        pivotLeft={pivotLeft}
        pivotRight={pivotRight}
        symbol={activeSymbol}
        symbols={symbolOptions}
        connected={connected}
        busy={selecting}
        onMarketChange={onMarketChange}
        onSymbolChange={setActiveSymbol}
        onSettingsChange={onSettingsChange}
        onConnect={onConnect}
        onStop={onStop}
      />

      <div className="terminal-grid">
        <Watchlist
          rows={rows}
          market={market}
          error={watchError}
          onSelect={onSelectFromWatchlist}
          onAdd={onAddToWatchlist}
          onRemove={onRemoveFromWatchlist}
          loading={selecting}
        />

        <div className="panel">
          <div className="chart-top">
            <span className="chart-symbol">{activeSymbol}</span>
            <span className="chart-price">{price != null ? price.toFixed(2) : "—"}</span>
            <span className="muted">{snap?.seed_source}</span>
            <span className="muted">{snap?.timeframe}</span>
            <div className="grow" />
            <TrendBadge signal={snap?.signal ?? null} />
          </div>
          <div className="chart-top chart-top-sub">
            <OverlayChips value={overlays} onChange={(k, on) => setOverlays((o) => ({ ...o, [k]: on }))} />
            {snap?.candle_time && <span className="muted">candle {snap.candle_time}</span>}
          </div>
          <Chart bars={bars} live={live} overlay={overlays} timeframe={snap?.timeframe ?? timeframe} />
        </div>

        <SignalPanel
          signal={snap?.signal ?? null}
          indicators={snap?.indicators ?? null}
          price={price}
          candleTime={snap?.candle_time ?? undefined}
        />
      </div>

      <AIAnalysis
        ai={ai}
        price={price}
        candleTime={snap?.candle_time ?? undefined}
        loading={aiLoading}
        llmCatalog={llmCatalog}
        llmProvider={llmProvider}
        llmModel={llmModel}
        llmApiKey={llmApiKey}
        onProviderChange={onLlmProviderChange}
        onModelChange={onLlmModelChange}
        onApiKeyChange={onLlmApiKeyChange}
        onRefresh={() => refreshAi(true, true)}
      />

      <SignalTable bars={bars} />

      {snap?.status && (
        <div className="banner warn" style={{ marginTop: 8 }}>
          Status: {snap.status} {snap.error ? `— ${snap.error}` : ""}
        </div>
      )}
    </div>
  );
}