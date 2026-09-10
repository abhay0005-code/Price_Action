"use client";

interface Props {
  marketOpen: boolean | null;
  wsConnected: boolean;
  backendUp: boolean | null;
}

export default function Header({ marketOpen, wsConnected, backendUp }: Props) {
  const live = backendUp === true && marketOpen === true;
  const cls =
    backendUp === false ? "off" : marketOpen ? "" : "closed";
  return (
    <header className="terminal-header">
      <div className="terminal-title">AI TRADING TERMINAL</div>
      <div className="row">
        {backendUp === false && <span className="muted">backend offline</span>}
        {wsConnected ? (
          <span className="muted">live feed</span>
        ) : (
          <span className="muted">no feed</span>
        )}
        <div className={`market-live ${cls}`}>
          <span className="dot" />
          {backendUp === false ? "OFFLINE" : marketOpen ? "MARKET LIVE" : "MARKET CLOSED"}
        </div>
      </div>
    </header>
  );
}