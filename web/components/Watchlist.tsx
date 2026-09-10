"use client";

import type { WatchRow } from "@/lib/api";
import { useState } from "react";

interface Props {
  rows: WatchRow[];
  market: "us" | "dhan";
  error?: string | null;
  onSelect: (symbol: string) => void;
  onAdd: (symbol: string) => Promise<void>;
  onRemove: (symbol: string) => Promise<void>;
  loading?: boolean;
}

function fmtPrice(p: number | null): string {
  return p == null || !Number.isFinite(p) ? "—" : p.toFixed(2);
}

function fmtPct(p: number | null): string {
  if (p == null || !Number.isFinite(p)) return "—";
  const sign = p > 0 ? "+" : "";
  return `${sign}${p.toFixed(2)}%`;
}

export default function Watchlist({ rows, market, error, onSelect, onAdd, onRemove, loading }: Props) {
  const [symbol, setSymbol] = useState("");
  const defaultSymbols = market === "us"
    ? ["AAPL", "NVDA", "TSLA", "MSFT", "SPY", "QQQ"]
    : ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN"];

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const value = symbol.trim().toUpperCase();
    if (!value) return;
    await onAdd(value);
    setSymbol("");
  };

  return (
    <div className="panel">
      <div className="panel-title">
        <span>Watchlist</span>
        {loading && <span className="spinner" />}
      </div>
      <form className="watchlist-add" onSubmit={submit}>
        <input
          value={symbol}
          onChange={(event) => setSymbol(event.target.value)}
          placeholder={market === "us" ? "Add US symbol" : "Add NSE symbol"}
          aria-label="Symbol to add"
          spellCheck={false}
        />
        <button type="submit" className="control-button primary" title="Add symbol" disabled={!symbol.trim()}>+</button>
      </form>
      <div className="panel-body" style={{ padding: "4px 6px" }}>
        {error ? (
          <div className="muted" style={{ padding: 8 }}>
            {error}
          </div>
        ) : (
          rows.map((r) => {
            const chg = r.changePct;
            const cls = chg == null ? "" : chg > 0 ? "up" : chg < 0 ? "down" : "";
            return (
              <div
                key={r.symbol}
                className={`wl-row ${r.active ? "active" : ""}`}
                onClick={() => onSelect(r.symbol)}
              >
                <span className="wl-symbol">{r.symbol}</span>
                <span className="wl-price">{fmtPrice(r.price)}</span>
                <span className={`wl-change ${cls}`}>{fmtPct(chg)}</span>
                {!defaultSymbols.includes(r.symbol) && (
                  <button
                    type="button"
                    className="wl-remove"
                    title={`Remove ${r.symbol}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      void onRemove(r.symbol);
                    }}
                  >
                    ×
                  </button>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}