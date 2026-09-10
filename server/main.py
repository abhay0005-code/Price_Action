"""FastAPI backend for the AI Trading Terminal (Next.js frontend).

Runs the existing Python engine (Alpaca live feed) behind a small JSON API a
React/Next.js client can consume.

Run with:
    uvicorn server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from server.engine import DEFAULT_TIMEFRAME, manager
from ai_signal import LLMJudge, list_llm_providers, llm_models_for, llm_default_model

log = logging.getLogger("terminal.api")

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
    return {
        "providers": providers,
        "default_provider": (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower(),
        "default_model": os.environ.get("LLM_MODEL") or "",
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