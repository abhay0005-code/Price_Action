"use client";

import type { Indicators, Signal } from "@/lib/api";

interface Props {
  signal: Signal | null;
  indicators: Indicators | null;
  price: number | null;
  candleTime?: string;
}

function fmt(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

function sideClass(side: string | null): string {
  const s = (side ?? "").toLowerCase();
  if (s.includes("long") || s.includes("bull")) return "long";
  if (s.includes("short") || s.includes("bear")) return "short";
  return "watch";
}

export default function SignalPanel({ signal, indicators, price, candleTime }: Props) {
  const side = signal?.side ?? "WAIT";
  const conf = signal?.confidence ?? null;
  const confPct = conf == null ? null : Math.round(conf * 100);
  const confColor = conf == null ? "#64748b" : conf >= 0.6 ? "#22c55e" : conf >= 0.45 ? "#eab308" : "#ef4444";

  const rows: [string, string][] = [
    ["Strategy", signal?.strategy ?? "—"],
    ["RVOL", fmt(indicators?.rvol, 2)],
    ["RSI", fmt(indicators?.rsi, 1)],
    ["EMA 9", fmt(indicators?.ema9)],
    ["EMA 21", fmt(indicators?.ema21)],
    ["EMA 169", fmt(indicators?.ema169)],
    ["VWAP", fmt(indicators?.vwap)],
    ["Support", fmt(indicators?.support)],
    ["Resistance", fmt(indicators?.resistance)],
    ["BOS", indicators?.bos ?? "—"],
    ["FVG", indicators?.fvg?.side ? `${indicators.fvg.side} (${indicators.fvg_state ?? ""})` : "none"],
  ];

  return (
    <div className="panel">
      <div className="panel-title">
        <span>Signal</span>
        {candleTime && <span className="muted">{candleTime}</span>}
      </div>
      <div className="panel-body">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
          <span className={`side-badge ${sideClass(side)}`}>{side}</span>
          {price != null && <span className="chart-price">{price.toFixed(2)}</span>}
        </div>
        {conf != null && (
          <div className="metric" style={{ borderBottom: "1px solid var(--border)" }}>
            <span className="k">Confidence</span>
            <span style={{ color: confColor, fontWeight: 700 }}>{confPct}%</span>
          </div>
        )}
        {conf != null && (
          <div className="confidence-bar">
            <div className="confidence-fill" style={{ width: `${confPct}%`, background: confColor }} />
          </div>
        )}
        {rows.map(([k, v]) => (
          <div className="metric" key={k}>
            <span className="k">{k}</span>
            <span>{v}</span>
          </div>
        ))}
        {signal?.reason && <div className="reason">{signal.reason}</div>}
      </div>
    </div>
  );
}