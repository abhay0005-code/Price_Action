"""FastAPI backend for the AI Trading Terminal (Next.js frontend).

Runs the existing Python engine (Alpaca live feed) behind a small JSON API a
React/Next.js client can consume.

Run with:
    uvicorn server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine import DEFAULT_TIMEFRAME, manager
from ai_signal import LLMJudge, list_llm_providers, llm_models_for, llm_default_model

log = logging.getLogger("terminal.api")

# Directory containing the statically-exported Next.js frontend (web/out).
# Override with the WEB_STATIC_DIR env var if the UI lives elsewhere.
WEB_STATIC_DIR = Path(
    os.environ.get(
        "WEB_STATIC_DIR",
        str(Path(__file__).resolve().parent.parent / "web" / "out"),
    )
)

# ---------------------------------------------------------------------------
# Running Jesse instance (the strategy builder data source). The terminal pulls
# the strategy catalog straight off the live `jesse run` server (default
# http://localhost:9000) and copies the code into a local folder so the repo
# keeps a plain-text mirror of every strategy. Override with env vars.
JESSE_URL = os.environ.get("JESSE_URL", "http://localhost:9000").rstrip("/")
JESSE_PASSWORD = os.environ.get("JESSE_PASSWORD", "secret")
JESSE_STRATEGY_DIR = Path(
    os.environ.get(
        "JESSE_STRATEGY_DIR",
        str(Path(__file__).resolve().parent.parent / "Jesse_Strategy"),
    )
)

app = FastAPI(title="AI Trading Terminal API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production deployments
    allow_methods=["*"],
    allow_headers=["*"],
)


class SelectRequest(BaseModel):
    symbol: str
    market: str = "us"
    provider: str = ""
    timeframe: str = DEFAULT_TIMEFRAME
    strategy: str = "price_action"
    fast_ema: int = 20
    slow_ema: int = 50
    pivot_left: int = 3
    pivot_right: int = 3


@app.get("/api/health")
def health():
    return manager.health()


@app.get("/api/symbols")
def preset_symbols(market: str = "us"):
    return manager.preset_symbols(market)


@app.get("/api/watchlist")
def watchlist(market: str = "us"):
    return manager.watchlist(market)


class WatchlistRequest(BaseModel):
    market: str = "us"
    symbol: str


@app.post("/api/watchlist")
def add_watchlist(req: WatchlistRequest):
    try:
        return manager.add_to_watchlist(req.market, req.symbol)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/watchlist")
def remove_watchlist(req: WatchlistRequest):
    try:
        return manager.remove_from_watchlist(req.market, req.symbol)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/select")
def select(req: SelectRequest):
    try:
        if (req.market or "us").strip().lower() == "dhan":
            res = manager.select_dhan(
                req.symbol,
                req.timeframe,
                req.strategy,
                req.fast_ema,
                req.slow_ema,
                req.pivot_left,
                req.pivot_right,
            )
        elif (req.market or "").strip().lower() == "delta":
            res = manager.select_delta(
                req.symbol,
                req.timeframe,
                req.strategy,
                req.fast_ema,
                req.slow_ema,
                req.pivot_left,
                req.pivot_right,
            )
        else:
            res = manager.select(
                req.symbol,
                req.timeframe,
                req.strategy,
                req.provider,
                req.fast_ema,
                req.slow_ema,
                req.pivot_left,
                req.pivot_right,
            )
        return {"ok": True, **res}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/engine/snapshot")
def engine_snapshot(symbol: str | None = None):
    try:
        return manager.snapshot_payload(symbol)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/engine/candles")
def engine_candles(
    symbol: str | None = None,
    limit: int = Query(160, ge=20, le=500),
):
    try:
        return manager.candles(symbol, limit)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/engine/ai")
def engine_ai(symbol: str | None = None, refresh: bool = False):
    try:
        return manager.ai_payload(symbol, refresh)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


class LlmSelectRequest(BaseModel):
    provider: str = "ollama"
    model: str = ""
    api_key: str = ""


@app.get("/api/llm/providers")
def llm_providers():
    providers = list_llm_providers()
    default_provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    default_model = (os.environ.get("LLM_MODEL") or "").strip() or llm_default_model(default_provider)
    return {
        "providers": providers,
        "default_provider": default_provider,
        "default_model": default_model,
        "models": {p: llm_models_for(p) for p in providers},
    }


@app.post("/api/llm/select")
def llm_select(req: LlmSelectRequest):
    try:
        return manager.set_llm(req.provider, req.model, req.api_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/llm/check")
def llm_check(req: LlmSelectRequest):
    try:
        judge = LLMJudge(req.provider, req.model, req.api_key)
        result = judge.judge({}, "HOLD", 0.0, "connection check")
        if "signal" not in result:
            raise RuntimeError(result.get("reason", "LLM connection failed"))
        return {"ok": True, "provider": result["provider"], "model": result["model"]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/strategies/jesse/list")
def jesse_strategy_list():
    """List the strategy names available on the running Jesse instance."""
    jesse_url = os.environ.get("JESSE_URL", "http://localhost:9000").rstrip("/")
    password = os.environ.get("JESSE_PASSWORD", "secret")
    token = hashlib.sha256(password.encode("utf-8")).hexdigest()
    try:
        r = httpx.get(f"{jesse_url}/strategy/all", headers={"Authorization": token}, timeout=10.0)
        r.raise_for_status()
        data = r.json()
        return {"ok": True, "strategies": data.get("strategies", [])}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jesse unreachable: {exc}")


@app.post("/api/strategies/jesse/pull")
def jesse_strategy_pull():
    """Fetch every strategy from the running Jesse and copy the code into the
    local Jesse_Strategy folder (one plain-text .py file per strategy)."""
    jesse_url = os.environ.get("JESSE_URL", "http://localhost:9000").rstrip("/")
    password = os.environ.get("JESSE_PASSWORD", "secret")
    token = hashlib.sha256(password.encode("utf-8")).hexdigest()
    header = {"Authorization": token}
    dest_dir = Path(
        os.environ.get(
            "JESSE_STRATEGY_DIR",
            str(Path(__file__).resolve().parent.parent / "Jesse_Strategy"),
        )
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        r = httpx.get(f"{jesse_url}/strategy/all", headers=header, timeout=10.0)
        r.raise_for_status()
        names = r.json().get("strategies", [])
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jesse unreachable: {exc}")

    pulled = []
    for name in names:
        try:
            g = httpx.post(
                f"{jesse_url}/strategy/get",
                json={"name": name},
                headers=header,
                timeout=15.0,
            )
            g.raise_for_status()
            content = g.json().get("content", "")
        except Exception as exc:
            pulled.append({"name": name, "error": str(exc)})
            continue
        target = dest_dir / f"{name}.py"
        target.write_text(content, encoding="utf-8")
        pulled.append({"name": name, "file": target.name, "bytes": len(content)})
    return {"ok": True, "target": str(dest_dir), "strategies": pulled}


@app.post("/api/stop")
def stop_engine():
    manager.stop()
    return {"ok": True, "status": "stopped"}


@app.get("/api/engine/order-preview")
def engine_order_preview(
    symbol: str | None = None,
    quantity: int = Query(5, ge=1, le=1_000_000),
    sl_pct: float = Query(2.0, gt=0, le=100),
):
    try:
        return manager.order_preview(symbol, quantity, sl_pct)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.websocket("/ws")
async def ws_feed(websocket: WebSocket):
    """Push live updates for the active engine every ~1s."""
    await websocket.accept()
    try:
        while True:
            try:
                payload = await asyncio.to_thread(manager.ws_payload)
                await websocket.send_json(payload)
            except Exception as exc:  # keep the socket alive on transient errors
                await websocket.send_json({"type": "error", "message": str(exc)})
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.on_event("shutdown")
def _shutdown():
    manager.stop()
    log.info("terminal backend stopped")


# ---------------------------------------------------------------------------
# Serve the statically-exported Next.js frontend. Mounted LAST so the JSON API
# (/api/...) and the WebSocket (/ws) keep winning, and everything else (the UI
# at "/" plus its assets) is served from web/out.
if WEB_STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_STATIC_DIR, html=True), name="frontend")
    log.info("serving frontend from %s", WEB_STATIC_DIR)


@app.get("/", include_in_schema=False)
def root_no_build() -> dict:
    return {
        "name": "AI Trading Terminal API",
        "detail": (
            "The web UI has not been built yet. Run "
            "`cd web && npm install && npm run build` (produces web/out), "
            "or point WEB_STATIC_DIR at a folder containing an exported build."
        ),
        "api": "/api/health",
        "health": "/api/health",
    }