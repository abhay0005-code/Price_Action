"""Live Delta Exchange India feed (crypto). Public REST + WS, no auth needed."""
from __future__ import annotations
import os, json, threading, time
from datetime import datetime, timedelta
from typing import Any
import pandas as pd
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import httpx, websocket
from strategy import PriceActionStrategy
from trend_reversal import TrendReversalStrategy
from breakout_breakdown import BreakoutBreakdownStrategy
from ai_signal import AISignalEngine
load_dotenv()
UTC = ZoneInfo("UTC")
DELTA_BASE = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
DELTA_WS_URL = os.environ.get("DELTA_WS_URL", "wss://socket.india.delta.exchange")
TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60}
RESOLUTION = {"5m": "5m", "15m": "15m", "1h": "1h"}
DELTA_PRESETS = ["BTCUSD","ETHUSD","SOLUSD","XRPUSD","DOGEUSD","BNBUSD","ADAUSD","AVAXUSD","LINKUSD","MATICUSD"]
_prod_cache: tuple[float, list] | None = None
def _products() -> list:
    global _prod_cache
    now = time.time()
    if _prod_cache and now - _prod_cache[0] < 600:
        return _prod_cache[1]
    r = httpx.get(f"{DELTA_BASE}/v2/products", timeout=15.0)
    r.raise_for_status()
    d = r.json()
    prods = d.get("result", []) if isinstance(d, dict) else []
    _prod_cache = (now, prods)
    return prods
def resolve_symbol(symbol: str) -> dict[str, Any]:
    q = (symbol or "").strip().upper().replace(" ","").replace("-","").replace("/","").replace("_","")
    if not q:
        raise ValueError("symbol must not be empty")
    for sfx in ("USDT","USDTPERP","PERP"):
        if q.endswith(sfx) and len(q) > len(sfx):
            q = q[: -len(sfx)] + "USD"
            break
    if not (q.endswith("USD") or q.endswith("INR")):
        q = f"{q}USD"
    try:
        up = {str(p.get("symbol","")).upper(): p for p in _products() if p.get("symbol")}
        if q in up:
            p = up[q]
            return {"symbol": q, "display": q, "instrument_type": p.get("contract_type","crypto"), "product_id": p.get("id")}
    except Exception:
        pass
    return {"symbol": q, "display": q, "instrument_type": "crypto"}
def fetch_tickers(symbols: list[str] | None = None) -> dict[str, dict]:
    r = httpx.get(f"{DELTA_BASE}/v2/tickers", timeout=12.0)
    r.raise_for_status()
    d = r.json()
    rows = d.get("result", []) if isinstance(d, dict) else []
    want = {s.upper() for s in (symbols or [])}
    out: dict[str, dict] = {}
    for t in rows:
        sym = str(t.get("symbol","")).upper()
        if want and sym not in want:
            continue
        out[sym] = t
    return out
def fetch_candles(symbol: str, resolution: str, start: int, end: int) -> list:
    r = httpx.get(f"{DELTA_BASE}/v2/history/candles", params={"symbol": symbol, "resolution": resolution, "start": start, "end": end}, timeout=15.0)
    r.raise_for_status()
    d = r.json()
    res = d.get("result", []) if isinstance(d, dict) else d
    return res or []
def ticker_price(t: dict) -> float | None:
    for k in ("close","mark_price","spot_price","last_price"):
        try:
            v = float(t.get(k)) if t.get(k) is not None else None
        except (TypeError, ValueError):
            v = None
        if v and v > 0:
            return v
    q = t.get("quotes") or {}
    for k in ("best_bid","best_ask"):
        try:
            v = float(q.get(k)) if q.get(k) is not None else None
        except (TypeError, ValueError):
            v = None
        if v and v > 0:
            return v
    return None
class DeltaIndiaLiveEngine:
    provider_name = "delta"; seed_provider = "delta"
    def __init__(self, symbol="BTCUSD", timeframe="5m", strategy="price_action", fast_ema=20, slow_ema=50, pivot_left=3, pivot_right=3, seed_days=7):
        if timeframe not in TIMEFRAME_MINUTES: raise ValueError("timeframe must be 5m/15m/1h")
        r = resolve_symbol(symbol)
        self.symbol=r["symbol"]; self.display=r["display"]; self.instrument_type=r["instrument_type"]
        self.timeframe=timeframe; self.minutes=TIMEFRAME_MINUTES[timeframe]; self.seed_days=int(seed_days); self.seed_source="unknown"
        sl=strategy.lower()
        if sl in ("trend","trend reversal","trend_reversal"): self.strategy=TrendReversalStrategy()
        elif "breakout" in sl: self.strategy=BreakoutBreakdownStrategy()
        else: self.strategy=PriceActionStrategy(timeframe,fast_ema,slow_ema,pivot_left,pivot_right)
        self.history=None; self.current=None; self._thread=None; self._ws=None
        self.status="idle"; self.error=""; self.lock=threading.Lock(); self._stop=threading.Event(); self._ws_ready=threading.Event()
        self.ai_engine=AISignalEngine(buy_threshold=0.20,sell_threshold=-0.20)
        self._ai_llm_applied=("", "", "")
        self._ai_key=None; self._ai_value=None; self._ai_computing=False; self._ai_force=False; self._ai_history=[]; self._last_closed=None
        self.last_order_text=""; self.placed_fingerprint=None
    @property
    def market(self): return "delta"
    def check_llm_now(self): return "checked"
    def start(self):
        seed=self._seed()
        if not seed: seed=self._synth()
        if not seed: raise RuntimeError("Delta: unable to fetch candles for this symbol")
        with self.lock:
            self.history=pd.DataFrame(seed); self._ingest()
        self.status=f"LIVE {self.display} via Delta {self.timeframe} | connecting..."
        try: self._open_ws()
        except Exception as e:
            self._stop.clear(); self._thread=threading.Thread(target=self._poll_forever,daemon=True); self._thread.start()
            self.status=f"LIVE {self.display} via Delta {self.timeframe} | polling ({e})"
        return self.status
    def stop(self):
        self._stop.set()
        try:
            if self._ws: self._ws.close()
        except Exception: pass
        self._ws=None
        if self._thread: self._thread.join(timeout=2.0); self._thread=None
        self.status="stopped"
    def _seed(self):
        try:
            end=int(time.time()); start=int((datetime.now(UTC)-timedelta(days=max(1,self.seed_days))).timestamp())
            rows=fetch_candles(self.symbol,RESOLUTION.get(self.timeframe,"5m"),start,end)
        except Exception: return None
        out=[]
        for r in rows or []:
            try: t,o,h,l,c=r[0],r[1],r[2],r[3],r[4]; v=r[5] if len(r)>5 else 0
            except Exception: continue
            try: out.append({"timestamp":datetime.fromtimestamp(int(t),tz=UTC),"open":float(o),"high":float(h),"low":float(l),"close":float(c),"volume":float(v or 0)})
            except Exception: continue
        if out: self.seed_source="delta"
        return out or None
    def _synth(self):
        try: px=ticker_price((fetch_tickers([self.symbol]).get(self.symbol.upper()) or {}))
        except Exception: px=None
        if not px: return None
        now=datetime.now(UTC).replace(second=0,microsecond=0); rows=[]
        for i in range(220,0,-1):
            ts=now-timedelta(minutes=self.minutes*i)
            rows.append({"timestamp":ts,"open":float(px),"high":float(px),"low":float(px),"close":float(px),"volume":0.0})
        self.seed_source="delta-ticker (synthetic)"; return rows
    def _bucket(self,ts): return ts.replace(minute=(ts.minute//self.minutes)*self.minutes,second=0,microsecond=0)
    def _on_price(self,price,ts=None):
        if price<=0: return
        now=ts or datetime.now(UTC)
        if now.tzinfo is None: now=now.replace(tzinfo=UTC)
        b=self._bucket(now)
        with self.lock:
            if self.history is None: return
            cur=self.current
            if cur is None or cur["start"]!=b:
                if cur is not None: self._close(cur)
                self.current={"start":b,"timestamp":b+timedelta(minutes=self.minutes),"open":price,"high":price,"low":price,"close":price,"ticks":1}
            else: cur["high"]=max(cur["high"],price); cur["low"]=min(cur["low"],price); cur["close"]=price; cur["ticks"]=cur.get("ticks",0)+1
    def _ingest(self):
        if self.history is None or self.history.empty: return
        last=self.history.iloc[-1]
        lts=pd.Timestamp(last["timestamp"]).to_pydatetime()
        if lts.tzinfo is None: lts=lts.replace(tzinfo=UTC)
        nb=self._bucket(datetime.now(UTC))
        if self._bucket(lts)>=nb: self.history=self.history.iloc[:-1].reset_index(drop=True)
        px=float(last["close"])
        self.current={"start":nb,"timestamp":nb+timedelta(minutes=self.minutes),"open":px,"high":px,"low":px,"close":px,"ticks":0}
        self.history=self.history.reset_index(drop=True)
    def _close(self,c):
        row={"timestamp":c["start"],"open":c["open"],"high":c["high"],"low":c["low"],"close":c["close"],"volume":float(c.get("ticks",0))}
        self.history=pd.concat([self.history,pd.DataFrame([row])],ignore_index=True)
        try:
            sig=self.strategy.generate_signal(self.history)
            last=sig.iloc[-1]
            side=str(last.get("signal","HOLD")).upper()
            if side not in ("BUY","SELL","HOLD"): side="HOLD"
            self._last_closed={"signal":side,"price":float(row["close"]),"timestamp":str(row["timestamp"])}
        except Exception: pass
        self._ai_force=True
    def _open_ws(self):
        self._stop.clear(); self._ws_ready.clear(); eng=self
        def on_open(ws):
            try: ws.send(json.dumps({"type":"subscribe","payload":{"channels":[{"name":"v2/ticker","symbols":[eng.symbol]}]}}))
            except Exception: pass
            eng._ws_ready.set()
        def on_msg(ws,msg):
            try: m=json.loads(msg)
            except Exception: return
            px=None
            for k in ("close","price","mark_price","last_price","p"):
                try:
                    if m.get(k) is not None: px=float(m[k]); break
                except Exception: continue
            if px and px>0: eng._on_price(float(px),None)
        ws=websocket.WebSocketApp(DELTA_WS_URL,on_open=on_open,on_message=on_msg)
        self._ws=ws; self._thread=threading.Thread(target=ws.run_forever,kwargs={"ping_interval":20},daemon=True); self._thread.start()
        if not self._ws_ready.wait(timeout=8.0): raise RuntimeError("Delta WS timeout")
        self.status=f"LIVE {self.display} via Delta {self.timeframe} | stream connected"
        threading.Thread(target=self._gapfill,daemon=True).start()
    def _gapfill(self):
        while not self._stop.is_set():
            time.sleep(15.0)
            if self._stop.is_set(): break
            try:
                px=ticker_price((fetch_tickers([self.symbol]).get(self.symbol.upper()) or {}))
                if px: self._on_price(float(px),None)
            except Exception: pass
    def _poll_forever(self):
        while not self._stop.is_set():
            try:
                px=ticker_price((fetch_tickers([self.symbol]).get(self.symbol.upper()) or {}))
                if px: self._on_price(float(px),None)
            except Exception: pass
            time.sleep(2.0)
    def snapshot(self,limit=200):
        with self.lock:
            if self.history is None: return None,[]
            fr=self.history.copy(); live=dict(self.current) if self.current else None; lc=dict(self._last_closed) if self._last_closed else None
        if fr.empty: return None,[]
        cl=fr["close"].astype(float)
        e9=cl.ewm(span=9,adjust=False).mean().iloc[-1]; e21=cl.ewm(span=21,adjust=False).mean().iloc[-1]
        e169=cl.ewm(span=169,adjust=False).mean().iloc[-1] if len(cl)>=2 else cl.iloc[-1]
        if lc is None:
            try:
                sig=self.strategy.generate_signal(fr)
                last=sig.iloc[-1]
                side=str(last.get("signal","HOLD")).upper()
                if side not in ("BUY","SELL","HOLD"): side="HOLD"
            except Exception: side="HOLD"
            lc={"signal":side,"price":float(fr["close"].iloc[-1]),"timestamp":str(fr["timestamp"].iloc[-1])}
        lp=float(live["close"]) if live else float(fr["close"].iloc[-1])
        summary={"live_price":lp,"candle_time":str(fr["timestamp"].iloc[-1]),"closes_at":str(live["timestamp"]) if live else "","position":lc.get("signal","HOLD"),"signal":type(self.strategy).__name__,"trend":"","reason":"","indicators":{"ema9":float(e9),"ema21":float(e21),"ema169":float(e169),"vwap":None,"rsi":None}}
        tail=fr.tail(limit); ct=tail["close"].astype(float)
        a=ct.ewm(span=9,adjust=False).mean(); b=ct.ewm(span=21,adjust=False).mean(); c=ct.ewm(span=169,adjust=False).mean()
        bars=[]
        for i,(_,r) in enumerate(tail.iterrows()):
            bars.append({"time":str(r["timestamp"]),"t":int(pd.Timestamp(r["timestamp"]).timestamp()),"open":float(r["open"]),"high":float(r["high"]),"low":float(r["low"]),"close":float(r["close"]),"volume":float(r.get("volume",0)),"ema9":float(a.iloc[i]),"ema21":float(b.iloc[i]),"ema169":float(c.iloc[i]),"vwap":None,"rsi":None,"signal":None,"trend":None,"position":None})
        return summary,bars
    def set_llm(self, provider=None, model=None, api_key=None):
        sig=((provider or "").strip(),(model or "").strip(),(api_key or "").strip())
        if sig!=self._ai_llm_applied:
            self.ai_engine.set_llm(provider=provider,model=model,api_key=api_key)
            self._ai_llm_applied=sig; self._ai_force=True
    def ai_signal(self,force=False):
        with self.lock:
            if self.history is None: return {"status":"idle","note":"no data yet"}
            latest=self.history["timestamp"].iloc[-1]; fr=self.history.copy()
        force=force or self._ai_force; self._ai_force=False
        if not force and self._ai_key==latest: return self._ai_value or {"status":"computing"}
        if self._ai_computing: return {"status":"computing","note":"AI analysis in progress"}
        self._ai_computing=True
        try: res=self.ai_engine.analyze(fr); res["status"]="ok"
        except Exception as e: res={"status":"error","note":str(e)}
        finally: self._ai_computing=False
        self._ai_key=latest; self._ai_value=res
        if res.get("status")=="ok":
            self._ai_history.append({"timestamp":res["timestamp"],"signal":res["signal"],"confidence":round(float(res["confidence"]),2),"score":round(float(res["score"]),2)}); self._ai_history=self._ai_history[-200:]
        res["history"]=list(self._ai_history); return res
    def ai_snapshot(self,force=False):
        return self.ai_signal(force=force)
    def trade_preview(self,qty=5,sl=2.0):
        with self.lock: lc=self._last_closed
        if lc is None or lc.get("signal") not in ("BUY","SELL"): return f"{self.display} - no paper trade trigger yet."
        p=lc["price"]; side=lc["signal"]; st=round(p*(1+sl/100.0) if side=="SELL" else p*(1-sl/100.0),2)
        return "\n".join(["--- PAPER TRADE (Delta India) ---",f"Symbol: {self.display}",f"Signal: {side} @ {p:.2f}",f"TF: {self.timeframe}",f"Action: {side} {qty}",f"Entry: {p:.2f}",f"Stop: {st:.2f}","Paper only."])
    def place_signal_order(self,qty=5,sl=2.0): return self.trade_preview(qty,sl)
    def place_option_order(self,sid,lots=1,sl=2.0): return "Delta options not wired (paper only)."

