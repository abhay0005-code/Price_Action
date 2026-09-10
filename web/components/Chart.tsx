"use client";

import { useEffect, useRef } from "react";
import {
  ColorType,
  createChart,
  IChartApi,
  ISeriesApi,
  LineStyle,
  UTCTimestamp,
} from "lightweight-charts";
import type { Bar, WsUpdate } from "@/lib/api";

export type OverlayKey = "ema9" | "ema21" | "ema169" | "vwap";

const COLORS: Record<OverlayKey, string> = {
  ema9: "#f59e0b",
  ema21: "#22d3ee",
  ema169: "#a855f7",
  vwap: "#64748b",
};

const LABELS: Record<OverlayKey, string> = {
  ema9: "EMA 9",
  ema21: "EMA 21",
  ema169: "EMA 169",
  vwap: "VWAP",
};

const OVERLAYS: OverlayKey[] = ["ema9", "ema21", "ema169", "vwap"];
const LIVE_BAR_MINS = 5; // matches the default 5m timeframe

interface Props {
  bars: Bar[];
  live: WsUpdate["live_candle"];
  overlay: Record<OverlayKey, boolean>;
  timeframe: string;
}

export default function Chart({ bars, live, overlay, timeframe }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeries = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lineSeries = useRef<Partial<Record<OverlayKey, ISeriesApi<"Line">>>>({});

  // create the chart once
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || chartRef.current) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#7d8b9c",
        fontFamily: "ui-monospace, Menlo, Consolas, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(34,48,63,0.4)" },
        horzLines: { color: "rgba(34,48,63,0.4)" },
      },
      rightPriceScale: { borderColor: "#22303f" },
      timeScale: { borderColor: "#22303f", timeVisible: true, secondsVisible: false },
      localization: { priceFormatter: (p: number) => p.toFixed(2) },
    });
    candleSeries.current = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });
    OVERLAYS.forEach((k) => {
      lineSeries.current[k] = chart.addLineSeries({
        color: COLORS[k],
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        lineStyle: k === "vwap" ? LineStyle.LargeDashed : LineStyle.Solid,
      });
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      candleSeries.current = null;
      lineSeries.current = {};
    };
  }, []);

  // full data refresh
  useEffect(() => {
    const chart = chartRef.current;
    const candles = candleSeries.current;
    if (!chart || !candles) return;
    candles.setData(
      bars.map((b) => ({ time: b.t as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close })),
    );
    OVERLAYS.forEach((k) => {
      const pts: { time: UTCTimestamp; value: number }[] = [];
      bars.forEach((b) => {
        const v = b[k];
        if (v != null && Number.isFinite(v)) pts.push({ time: b.t as UTCTimestamp, value: v });
      });
      lineSeries.current[k]?.setData(pts);
    });
    chart.timeScale().fitContent();
  }, [bars]);

  // overlay visibility
  useEffect(() => {
    OVERLAYS.forEach((k) => lineSeries.current[k]?.applyOptions({ visible: overlay[k] }));
  }, [overlay]);

  // live bar updates from WebSocket
  useEffect(() => {
    if (!live || !candleSeries.current) return;
    const minutes = timeframe === "1h" ? 60 : timeframe === "15m" ? 15 : LIVE_BAR_MINS;
    const time = Date.parse(live.closes_at) / 1000 - minutes * 60;
    candleSeries.current.update({
      time: time as UTCTimestamp,
      open: live.open,
      high: live.high,
      low: live.low,
      close: live.close,
    });
  }, [live]);

  return <div className="chart-wrap" ref={wrapRef} />;
}

export function OverlayChips({
  value,
  onChange,
}: {
  value: Record<OverlayKey, boolean>;
  onChange: (k: OverlayKey, on: boolean) => void;
}) {
  return (
    <div className="chip-row">
      {OVERLAYS.map((k) => (
        <button
          key={k}
          className={`chip ${value[k] ? "on" : ""}`}
          style={value[k] ? { color: COLORS[k] } : undefined}
          onClick={() => onChange(k, !value[k])}
        >
          {LABELS[k]}
        </button>
      ))}
    </div>
  );
}