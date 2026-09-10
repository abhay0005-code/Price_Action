/** Client helpers for the trading terminal backend.
 *
 * REST calls go through the same-origin Next.js proxy (`/api/proxy/...`) so
 * the browser never talks cross-origin to the Python backend. The live feed
 * uses a WebSocket straight to the FastAPI server (`NEXT_PUBLIC_WS_URL`).
 */

export const REST_BASE = "/api/proxy";

export function wsUrl(): string {
  if (typeof window === "undefined") return "";
  const fromEnv = process.env.NEXT_PUBLIC_WS_URL;
  if (fromEnv) return fromEnv && /\/ws$/.test(fromEnv) ? fromEnv : `${fromEnv}/ws`;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.hostname}:8000/ws`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${REST_BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.error ?? body.message ?? body;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(`backend returned ${res.status}`, String(detail), res.status);
  }
  return res.json() as Promise<T>;
}

export class ApiError extends Error {
  detail: string;
  status: number;
  constructor(message: string, detail: string, status: number) {
    super(message);
    this.detail = detail;
    this.status = status;
    Object.setPrototypeOf(this, ApiError.prototype);
  }
}

export interface MarketClock {
  open: boolean;
  as_of: string;
  next_open: string | null;
}

export interface MarketConfig {
  configured: boolean;
  alpaca?: boolean;
  finnhub?: boolean;
}

export interface EngineInfo {
  symbol: string;
  market: string;
  provider: string;
  timeframe: string;
  status: string;
  seed_source: string;
}

export interface Health {
  status: string;
  configured: boolean;
  markets: { us: MarketConfig; dhan: { configured: boolean } };
  market: string;
  provider: string;
  market_open: MarketClock;
  active_symbol: string;
  engine: EngineInfo | null;
}

export interface WatchRow {
  symbol: string;
  price: number | null;
  change: number | null;
  changePct: number | null;
  active: boolean;
}

export interface Watchlist {
  rows: WatchRow[];
  error?: string | null;
  configured: boolean;
  market?: string;
}

export interface Bar {
  time: string;
  t: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ema9: number | null;
  ema21: number | null;
  ema169: number | null;
  vwap: number | null;
  rsi: number | null;
  signal?: string | null;
  trend?: string | null;
  position?: string | null;
}

export interface Candles {
  symbol: string;
  timeframe: string;
  seed_source: string;
  bars: Bar[];
}

export interface Signal {
  side: string;
  strategy: string;
  trend: string;
  reason: string;
  confidence: number;
  source: string;
}

export interface FvgInfo {
  found: boolean;
  side: string | null;
}

export interface Indicators {
  ema9: number | null;
  ema21: number | null;
  ema169: number | null;
  vwap: number | null;
  rsi: number | null;
  rvol: number | null;
  support: number | null;
  resistance: number | null;
  bos: string;
  bos_side: string | null;
  fvg: FvgInfo;
  fvg_state: string | null;
}

export interface AiModel {
  active?: boolean;
  vote?: number;
  note?: string;
  forecast?: number;
  mean_forecast_pct?: number;
  vol_forecast_pct?: number;
  slope?: number;
  next_price?: number;
  p_buy?: number;
  p_sell?: number;
  p_hold?: number;
  [k: string]: unknown;
}

export interface AiHistoryRow {
  timestamp?: string;
  signal?: string;
  confidence?: number | null;
  score?: number | null;
}

export interface AiInfo {
  status?: string;
  note?: string;
  error?: string;
  symbol?: string;
  timestamp?: string;
  analyzed_at?: string;
  price?: number | null;
  signal?: string;
  confidence?: number | null;
  score?: number | null;
  reason?: string;
  indicators?: Record<string, unknown>;
  models?: Record<string, AiModel>;
  strategies?: Record<string, { signal?: string }>;
  llm?: { label?: string; signal?: string; confidence?: number; reason?: string; [k: string]: unknown } | null;
  llm_signal?: string | null;
  llm_enabled?: boolean;
  history?: AiHistoryRow[];
}

export interface Snapshot {
  status: string;
  symbol: string;
  display: string;
  timeframe: string;
  seed_source: string;
  market: string;
  provider: string;
  price: number;
  candle_time?: string;
  closes_at?: string;
  signal: Signal;
  indicators: Indicators;
  ai: AiInfo | null;
  error?: string;
}

export interface WsUpdate {
  type: "update" | "status" | "error";
  symbol?: string;
  market?: string;
  provider?: string;
  seed_source?: string;
  timeframe?: string;
  price?: number;
  candle_time?: string;
  closes_at?: string;
  status?: string;
  message?: string;
  error?: string;
  live_candle?: {
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    start: string;
    closes_at: string;
  } | null;
  signal?: { side: string; strategy: string; trend: string; reason: string };
}

export interface SymbolsList {
  market: string;
  symbols: string[];
}

// ------------------------------------------------------------------- API calls

export function health(): Promise<Health> {
  return request<Health>("/health");
}

export function symbols(market: string = "us"): Promise<SymbolsList> {
  return request<SymbolsList>(`/symbols?market=${encodeURIComponent(market)}`);
}

export function watchlist(market: string = "us"): Promise<Watchlist> {
  return request<Watchlist>(`/watchlist?market=${encodeURIComponent(market)}`);
}

export function addWatchlist(symbol: string, market: string): Promise<Watchlist> {
  return request<Watchlist>("/watchlist", {
    method: "POST",
    body: JSON.stringify({ symbol, market }),
  });
}

export function removeWatchlist(symbol: string, market: string): Promise<Watchlist> {
  return request<Watchlist>("/watchlist", {
    method: "DELETE",
    body: JSON.stringify({ symbol, market }),
  });
}

export interface SelectResult {
  ok: boolean;
  symbol: string;
  market: string;
  provider: string;
  status: string;
  engine: EngineInfo | null;
}

export interface SelectOptions {
  market?: string;
  provider?: string;
  timeframe?: string;
  strategy?: string;
  fastEma?: number;
  slowEma?: number;
  pivotLeft?: number;
  pivotRight?: number;
}

export function select(symbol: string, options: SelectOptions = {}): Promise<SelectResult> {
  return request<SelectResult>("/select", {
    method: "POST",
    body: JSON.stringify({
      symbol,
      market: options.market ?? "us",
      provider: options.provider ?? "",
      timeframe: options.timeframe ?? "5m",
      strategy: options.strategy ?? "price_action",
      fast_ema: options.fastEma ?? 20,
      slow_ema: options.slowEma ?? 50,
      pivot_left: options.pivotLeft ?? 3,
      pivot_right: options.pivotRight ?? 3,
    }),
  });
}

export function stop() {
  return request<{ ok: boolean; status: string }>("/stop", { method: "POST" });
}

export function orderPreview(symbol: string, quantity: number, slPct: number) {
  const q = `?symbol=${encodeURIComponent(symbol)}&quantity=${quantity}&sl_pct=${slPct}`;
  return request<{ symbol: string; quantity: number; sl_pct: number; preview: string; paper_only: boolean }>(
    `/engine/order-preview${q}`,
  );
}

export function snapshot(symbol?: string): Promise<Snapshot> {
  const q = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
  return request<Snapshot>(`/engine/snapshot${q}`);
}

export function candles(symbol?: string, limit = 200): Promise<Candles> {
  const q = symbol ? `?symbol=${encodeURIComponent(symbol)}&limit=${limit}` : `?limit=${limit}`;
  return request<Candles>(`/engine/candles${q}`);
}

export function aiAnalysis(symbol?: string, refresh = false): Promise<AiInfo> {
  const q = symbol
    ? `?symbol=${encodeURIComponent(symbol)}&refresh=${refresh}`
    : `?refresh=${refresh}`;
  return request<AiInfo>(`/engine/ai${q}`);
}

export interface LlmCatalog {
  providers: string[];
  default_provider: string;
  default_model: string;
  models: Record<string, string[]>;
}

export interface LlmSelection {
  provider?: string | null;
  model?: string | null;
  label?: string;
  configured?: boolean;
}

export function llmCatalog(): Promise<LlmCatalog> {
  return request<LlmCatalog>("/llm/providers");
}

export function llmSelect(provider: string, model: string, apiKey = ""): Promise<LlmSelection> {
  return request<LlmSelection>("/llm/select", {
    method: "POST",
    body: JSON.stringify({ provider, model, api_key: apiKey }),
  });
}

export function llmCheck(provider: string, model: string, apiKey = ""): Promise<{ ok: boolean; provider: string; model: string }> {
  return request<{ ok: boolean; provider: string; model: string }>("/llm/check", {
    method: "POST",
    body: JSON.stringify({ provider, model, api_key: apiKey }),
  });
}

export async function inferError(err: unknown): Promise<string> {
  if (err instanceof ApiError) return `${err.message}: ${err.detail}`;
  if (err instanceof Error) return err.message;
  return String(err);
}