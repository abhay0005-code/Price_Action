"use client";

import { useCallback, useEffect, useState } from "react";
import {
  jesseStrategiesList,
  jesseStrategiesPull,
  type JesseStrategyInfo,
} from "@/lib/api";

const JESSE_BASE = (process.env.NEXT_PUBLIC_JESSE_URL || "http://localhost:9000").replace(
  /\/+$/,
  ""
);
const JESSE_STRATEGIES_URL = `${JESSE_BASE}/#/strategies`;

export default function StrategyBuilder() {
  const [strategies, setStrategies] = useState<string[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [pulling, setPulling] = useState(false);
  const [pulled, setPulled] = useState<JesseStrategyInfo[] | null>(null);
  const [target, setTarget] = useState<string>("");
  const [pullError, setPullError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadingList(true);
    setListError(null);
    try {
      const res = await jesseStrategiesList();
      setStrategies(res.strategies ?? []);
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const pull = useCallback(async () => {
    setPulling(true);
    setPullError(null);
    setPulled(null);
    try {
      const res = await jesseStrategiesPull();
      setPulled(res.strategies ?? []);
      setTarget(res.target ?? "");
      if (res.strategies?.some((s) => s.error)) {
        setPullError("Some strategies failed to copy (see list).");
      }
      load();
    } catch (err) {
      setPullError(err instanceof Error ? err.message : String(err));
    } finally {
      setPulling(false);
    }
  }, [load]);

  return (
    <div className="strategy-builder">
      <div className="strategy-builder-bar">
        <span className="terminal-title">STRATEGY BUILDER</span>
        <span className="muted">
          Jesse live at{" "}
          <a href={JESSE_STRATEGIES_URL} target="_blank" rel="noreferrer">
            {JESSE_STRATEGIES_URL}
          </a>
        </span>
        <div className="grow" />
        <button className="btn" onClick={load} disabled={loadingList}>Refresh list</button>
        <button className="btn primary" onClick={pull} disabled={pulling}>
          {pulling ? "Pulling…" : "Pull all from URL"}
        </button>
      </div>

      {listError && <div className="banner warn">{listError}</div>}
      {pullError && <div className="banner warn">{pullError}</div>}
      {pulled && (
        <div className="banner ok">
          Copied {pulled.length} strateg{pulled.length === 1 ? "y" : "ies"} to{" "}
          <code>{target || "Jesse_Strategy"}</code>
        </div>
      )}

      <div className="strategy-builder-grid">
        <div className="panel strategy-builder-catalog">
          <div className="panel-title">Catalog on this terminal</div>
          <ul className="strategy-list">
            {loadingList ? (
              <li className="muted">loading…</li>
            ) : strategies.length === 0 ? (
              <li className="muted">none pulled yet</li>
            ) : (
              strategies.map((name) => <li key={name}>{name}</li>)
            )}
          </ul>
        </div>
        <div className="panel strategy-builder-frame-wrap">
          <iframe
            className="strategy-builder-frame"
            src={JESSE_STRATEGIES_URL}
            title="Jesse Strategy Builder"
          />
        </div>
      </div>
    </div>
  );
}
