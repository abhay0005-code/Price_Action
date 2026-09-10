"use client";

import type { AiInfo, AiModel, AiHistoryRow, LlmCatalog } from "@/lib/api";

interface Props {
  ai: AiInfo | null;
  price: number | null;
  candleTime?: string;
  loading: boolean;
  llmCatalog: LlmCatalog | null;
  llmProvider: string;
  llmModel: string;
  llmApiKey: string;
  onProviderChange: (provider: string) => void;
  onModelChange: (model: string) => void;
  onApiKeyChange: (apiKey: string) => void;
  onRefresh: () => void;
}

function fmt(v: number | null | undefined, digits = 3): string {
  return v == null || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

function pct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function vote(v: number | undefined): string {
  if (v === 1) return "BUY";
  if (v === -1) return "SELL";
  return "HOLD";
}

function sigClass(s: string | undefined): string {
  const v = (s ?? "").toUpperCase();
  if (v === "BUY") return "up";
  if (v === "SELL") return "down";
  return "side";
}

const EPSILON = 1e-9;

function extraFor(name: string, m: AiModel): string {
  const nameL = name.toLowerCase();
  if (nameL === "arima") return `forecast ${fmt(m.forecast, 2)}`;
  if (nameL === "garch")
    return `mean ${pct((m.mean_forecast_pct ?? 0) * 100)} vol ${pct((m.vol_forecast_pct ?? 0) * 100)}`;
  if (nameL === "kalman") return `slope ${fmt(m.slope)} next ${fmt(m.next_price, 2)}`;
  if (nameL === "xgboost" || nameL === "lstm")
    return `p_BUY ${fmt(m.p_buy)} p_SELL ${fmt(m.p_sell)} p_HOLD ${fmt(m.p_hold)}`;
  return "";
}

export default function AIAnalysis({
  ai, price, candleTime, loading, llmCatalog,
  llmProvider, llmModel, onProviderChange, onModelChange, onRefresh,
  llmApiKey, onApiKeyChange,
}: Props) {
  const ind = ai?.indicators ?? {};
  const models = ai?.models ?? {};
  const strategies = ai?.strategies ?? {};
  const history = ai?.history ?? [];

  const providers = llmCatalog?.providers ?? [];
  const modelChoices = llmCatalog?.models?.[llmProvider] ?? (llmModel ? [llmModel] : []);
  const providerOk = providers.includes(llmProvider);

  return (
    <div className="panel">
      <div className="panel-title">
        <span className="ai-title">AI SIGNAL</span>
        <span className="muted">auto on every candle close</span>
      </div>

      <div className="ai-llm-row">
        <label className="field">
          <span className="field-label">LLM Provider</span>
          <select value={llmProvider} onChange={(e) => onProviderChange(e.target.value)}>
            {!providerOk && <option value={llmProvider}>{llmProvider || "select…"}</option>}
            {providers.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">LLM Model</span>
          <select value={llmModel} onChange={(e) => onModelChange(e.target.value)}>
            {!modelChoices.includes(llmModel) && <option value={llmModel}>{llmModel || "select…"}</option>}
            {modelChoices.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </label>
        {llmProvider !== "ollama" && (
          <label className="field">
            <span className="field-label">API Key</span>
            <input
              type="password"
              value={llmApiKey}
              onChange={(e) => onApiKeyChange(e.target.value)}
              placeholder="Enter provider API key"
              autoComplete="off"
            />
          </label>
        )}
        <button className="ai-opt" onClick={onRefresh} disabled={loading}>
          {loading ? "…" : "Run AI Analysis"}
        </button>
      </div>
      <div className="panel-body ai-llm-hint muted">
        Ollama runs locally (no key). Others need their API key: GROQ_KEY /
        HF_API_TOKEN / OPENROUTER_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY in .env
      </div>

      <div className="ai-body" style={loading ? { opacity: 0.55 } : undefined}>
        {ai?.status === "computing" && <div className="ai-text muted">AI analysis in progress…</div>}

        {ai?.error && <div className="ai-text"><span className="down">⚠ {ai.error}</span></div>}

        {ai?.status !== "computing" && !ai?.error && (
          <>
            <div className="panel-body" style={{ borderBottom: "1px solid var(--border)" }}>
              {ai?.signal ? (
                <div className="row" style={{ gap: 14 }}>
                  <span className="ai-sig-label">AI SIGNAL</span>
                  <span className={`side-badge ${sigClass(ai.signal)}`}>{ai.signal}</span>
                  {ai.confidence != null && (
                    <span className="muted">confidence {fmt(ai.confidence, 2)}</span>
                  )}
                  {ai.confidence != null && ai.confidence > 0 && (
                    <span className="muted">{Math.round(ai.confidence * 100)}%</span>
                  )}
                </div>
              ) : (
                <div className="ai-text muted">
                  {ai ? ai.status ?? "waiting for closed candles" : `Select a symbol to request an AI read.`}
                </div>
              )}
            </div>

            {ai?.timestamp && (
              <div className="ai-text">
                <div>
                  TIMESTAMP: {ai.timestamp} {candleTime ? <span className="muted">(candle {candleTime})</span> : null}
                </div>
                <div>
                  PRICE: <b>{fmt(ai.price ?? price, 2)}</b>
                </div>
                {ai.signal && (
                  <div>
                    AI SIGNAL: <b className={sigClass(ai.signal)}>{ai.signal}</b>{" "}
                    {ai.confidence != null && <>confidence {fmt(ai.confidence, 2)}</>}{" "}
                    {ai.score != null && <>ensemble score {fmt(ai.score, 3)}</>}
                  </div>
                )}
                {ai.reason && <div>REASON: {ai.reason}</div>}

                {Object.keys(ind).length > 0 && (
                  <div>
                    <div className="muted">INDICATORS:</div>
                    <div>
                      RSI {fmt(ind.rsi as number | undefined, 1)} | EMA169{" "}
                      {fmt(ind.ema169 as number | undefined, 2)} | EMA9{" "}
                      {fmt(ind.ema9 as number | undefined, 2)} | EMA21{" "}
                      {fmt(ind.ema21 as number | undefined, 2)} | close-vs-169{" "}
                      {pct((ind.close_vs_ema169_pct as number | undefined ?? 0) * 100)} | vol-z{" "}
                      {fmt(ind.vol_z as number | undefined, 2)}
                    </div>
                  </div>
                )}

                {Object.keys(models).length > 0 && (
                  <div>
                    <div className="muted">MODEL VOTES:</div>
                    {Object.entries(models).map(([name, m]) => (
                      <div key={name}>
                        {name.toUpperCase()}{" ".repeat(Math.max(0, 10 - name.length))} {vote(m.vote)}{" "}
                        {extraFor(name, m)}{" "}
                        {!m.active && <span className="muted">(skipped {m.note ?? "n/a"})</span>}
                      </div>
                    ))}
                  </div>
                )}

                {Object.keys(strategies).length > 0 && (
                  <div>
                    <div className="muted">STRATEGY SIGNALS:</div>
                    {Object.entries(strategies).map(([name, st]) => (
                      <span key={name} style={{ marginRight: 14 }}>
                        {name}: <b className={sigClass(st.signal)}>{st.signal ?? "n/a"}</b>
                      </span>
                    ))}
                  </div>
                )}

                {ai.llm_enabled && ai.llm ? (
                  <div>
                    <div className="muted">LLM JUDGE:</div>
                    <div>
                      [{ai.llm.label ?? "llm"}]: {ai.llm.signal ?? ""}{" "}
                      {ai.llm.confidence != null && (
                        <> (conf {Math.round(ai.llm.confidence * 100)}%)</>
                      )}{" "}
                      - {ai.llm.reason ?? ""}
                    </div>
                  </div>
                ) : (
                  <div className="muted">
                    LLM: not configured (select a provider/model above, or set LLM_BASE_URL / LLM_MODEL in .env)
                  </div>
                )}

                {ai.analyzed_at && (
                  <div className="muted">Analyzed at {ai.analyzed_at}</div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {history.length > 0 && (
        <div className="panel-body" style={{ borderTop: "1px solid var(--border)", padding: "4px 8px" }}>
          <table className="ai-hist">
            <thead>
              <tr>
                <th>timestamp</th>
                <th>signal</th>
                <th>conf</th>
                <th>score</th>
              </tr>
            </thead>
            <tbody>
              {history.slice(-12).reverse().map((h: AiHistoryRow, i: number) => (
                <tr key={`${h.timestamp}-${i}`}>
                  <td className="muted">{String(h.timestamp ?? "").slice(0, 19)}</td>
                  <td className={sigClass(h.signal)}>{h.signal ?? "—"}</td>
                  <td>{h.confidence != null ? fmt(h.confidence, 2) : "—"}</td>
                  <td>{h.score != null ? fmt(h.score, 2) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}