"use client";

import type { Bar } from "@/lib/api";

interface Props {
  bars: Bar[];
}

function fmt(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

function signalClass(side: string): string {
  const s = (side || "").toUpperCase();
  if (s.includes("BUY") || s.includes("LONG")) return "up";
  if (s.includes("SELL") || s.includes("SHORT")) return "down";
  return "";
}

export default function SignalTable({ bars }: Props) {
  const recent = bars.slice(-20).reverse();

  if (recent.length === 0) {
    return (
      <div className="panel">
        <div className="panel-title">
          <span>Signal History</span>
          <span className="muted">closed candles</span>
        </div>
        <div className="panel-body">
          <div className="muted">No signals yet. Start a live feed to see signals here.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="panel-title">
        <span>Signal History</span>
        <span className="muted">last {recent.length} closed candles</span>
      </div>
      <div className="panel-body" style={{ padding: "4px 6px" }}>
        <table className="signal-table">
          <thead>
            <tr>
              <th>time</th>
              <th>close</th>
              <th>ema9</th>
              <th>ema21</th>
              <th>rsi</th>
              <th>signal</th>
              <th>trend</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((b, i) => (
              <tr key={`${b.t}-${i}`}>
                <td className="muted">{String(b.time).slice(11, 19)}</td>
                <td>{fmt(b.close)}</td>
                <td>{fmt(b.ema9)}</td>
                <td>{fmt(b.ema21)}</td>
                <td>{fmt(b.rsi, 1)}</td>
                <td className={signalClass(b.signal || "")}>{b.signal || "—"}</td>
                <td>{b.trend || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
