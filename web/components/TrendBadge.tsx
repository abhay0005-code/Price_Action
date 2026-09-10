"use client";

import type { Signal } from "@/lib/api";

const TREND_ICON: Record<string, string> = {
  UPTREND: "▲",
  DOWNTREND: "▼",
  SIDEWAYS: "►",
};

function cls(t: string): string {
  const v = (t || "").toUpperCase();
  if (v.includes("UP")) return "up";
  if (v.includes("DOWN")) return "down";
  return "side";
}

export default function TrendBadge({ signal }: { signal: Signal | null }) {
  if (!signal) return null;
  const trend = (signal.trend || "").toUpperCase() || "—";
  return (
    <div className="trend-badge">
      <span className={cls(trend)}>
        TREND {trend} {TREND_ICON[trend] ?? ""}
      </span>
      <span className={cls(signal.strategy)}>SIGNAL {signal.strategy || "—"}</span>
    </div>
  );
}