"use client";

import { useState } from "react";
import type { Health } from "@/lib/api";

export type Market = "us" | "dhan";
export type Provider = "alpaca" | "finnhub";

export const STRATEGIES = [
  { value: "price_action", label: "Price Action" },
  { value: "trend_reversal", label: "Trend Reversal" },
  { value: "breakout_breakdown", label: "Breakout / Breakdown" },
];

export const TIMEFRAMES = ["5m", "15m", "1h"];

interface Props {
  health: Health | null;
  market: Market;
  provider: Provider;
  timeframe: string;
  strategy: string;
  fastEma: number;
  slowEma: number;
  pivotLeft: number;
  pivotRight: number;
  symbol: string;
  symbols: string[];
  connected: boolean;
  busy: boolean;
  onMarketChange: (m: Market, p: Provider) => void;
  onSymbolChange: (s: string) => void;
  onSettingsChange: (s: {
    timeframe?: string;
    strategy?: string;
    fastEma?: number;
    slowEma?: number;
    pivotLeft?: number;
    pivotRight?: number;
  }) => void;
  onConnect: () => void;
  onStop: () => void;
}

function usConfigured(health: Health | null) {
  return health?.markets?.us?.configured ?? false;
}

function dhanConfigured(health: Health | null) {
  return health?.markets?.dhan?.configured ?? false;
}

export default function Toolbar(props: Props) {
  const {
    health, market, provider, timeframe, strategy,
    fastEma, slowEma, pivotLeft, pivotRight, symbol, symbols,
    connected, busy, onMarketChange: changeMarket, onSymbolChange: changeSymbol,
    onSettingsChange, onConnect, onStop,
  } = props;
  const [showParams, setShowParams] = useState(false);

  const us = usConfigured(health);
  const dhan = dhanConfigured(health);

  const pick = (m: Market, p: Provider) => {
    if (m === "us" && !us) return;
    if (m === "dhan" && !dhan) return;
    changeMarket(m, p);
  };

  return (
    <div className="toolbar panel">
      <div className="toolbar-row">
        <div className="seg" role="group" aria-label="data source">
          <button
            className={`seg-btn ${market === "us" && provider === "alpaca" ? "on" : ""}`}
            onClick={() => pick("us", "alpaca")}
            disabled={!us}
            title={us ? "US equities via Alpaca (IEX)" : "Alpaca keys not configured"}
          >
            US · Alpaca
          </button>
          <button
            className={`seg-btn ${market === "us" && provider === "finnhub" ? "on" : ""}`}
            onClick={() => pick("us", "finnhub")}
            disabled={!us}
            title={us ? "US equities via Finnhub (yfinance fallback)" : "FINNHUB_API_KEY not configured"}
          >
            US · Finnhub
          </button>
          <button
            className={`seg-btn ${market === "dhan" ? "on" : ""}`}
            onClick={() => pick("dhan", "finnhub")}
            disabled={!dhan}
            title={dhan ? "NSE equities via DhanHQ" : "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not configured"}
          >
            Dhan · NSE
          </button>
        </div>

        <label className="field">
          <span className="field-label">Symbol</span>
          <input
            list="preset-symbols"
            value={symbol}
            onChange={(e) => changeSymbol(e.target.value.toUpperCase())}
            onKeyDown={(e) => {
              if (e.key === "Enter") onConnect();
            }}
            spellCheck={false}
          />
          <datalist id="preset-symbols">
            {symbols.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </label>

        <label className="field">
          <span className="field-label">Timeframe</span>
          <select
            value={timeframe}
            onChange={(e) => onSettingsChange({ timeframe: e.target.value })}
          >
            {TIMEFRAMES.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field-label">Strategy</span>
          <select
            value={strategy}
            onChange={(e) => onSettingsChange({ strategy: e.target.value })}
          >
            {STRATEGIES.map((s) => (
              <option key={s.value} value={s.value}>{s.label}</option>
            ))}
          </select>
        </label>

        <button className={`param-toggle ${showParams ? "on" : ""}`} onClick={() => setShowParams(!showParams)}>
          Params
        </button>

        <div className="grow" />

        {connected ? (
          <button className="btn danger" onClick={onStop} disabled={busy}>Stop</button>
        ) : (
          <button className="btn primary" onClick={onConnect} disabled={busy}>
            {busy ? "Starting…" : "Start Live Feed"}
          </button>
        )}
      </div>

      {showParams && (
        <div className="toolbar-row params">
          <label className="field small">
            <span className="field-label">Fast EMA</span>
            <input
              type="number"
              value={fastEma}
              min={1}
              onChange={(e) => onSettingsChange({ fastEma: Number(e.target.value) })}
            />
          </label>
          <label className="field small">
            <span className="field-label">Slow EMA</span>
            <input
              type="number"
              value={slowEma}
              min={2}
              onChange={(e) => onSettingsChange({ slowEma: Number(e.target.value) })}
            />
          </label>
          <label className="field small">
            <span className="field-label">Pivot Left</span>
            <input
              type="number"
              value={pivotLeft}
              min={1}
              onChange={(e) => onSettingsChange({ pivotLeft: Number(e.target.value) })}
            />
          </label>
          <label className="field small">
            <span className="field-label">Pivot Right</span>
            <input
              type="number"
              value={pivotRight}
              min={1}
              onChange={(e) => onSettingsChange({ pivotRight: Number(e.target.value) })}
            />
          </label>
          <span className="muted params-hint">Reconnect to apply EMA / pivot changes</span>
        </div>
      )}

      {market === "dhan" && !dhan && (
        <div className="banner warn" style={{ margin: 8 }}>
          Dhan feed is not configured - set DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in .env
        </div>
      )}
      {market === "us" && !us && (
        <div className="banner warn" style={{ margin: 8 }}>
          No US data provider configured - set APCA_API_KEY_ID / APCA_API_SECRET_KEY or FINNHUB_API_KEY in .env
        </div>
      )}
    </div>
  );
}