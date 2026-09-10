"""AI buy/sell signal engine for the Price Action Trading Terminal.

Analyses the latest closed 5m candle with a stack of statistical / ML
models plus the app's rule strategies and (optionally) an LLM judge:

- Indicators  : 169 EMA, 9/21/50 EMA, RSI, volatility, volume z-score,
                 candle shape and range-breakout flags.
- ARIMA(1,1,1): one-step-ahead forecast of log price -> direction vote.
- GARCH(1,1)  : AR(1) conditional mean + volatility forecast -> vote.
- Kalman      : local-linear-trend filter -> trend/slope vote.
- XGBoost     : gradient boosted classifier trained on the actual data
                 (features at bar t -> sign of the next-bar return).
- Strategies  : Price Action / Trend Reversal / Breakout & Breakdown.
- LLM judge   : optional; sends a compact context of all the above to an
                 OpenAI-compatible Chat Completions endpoint and asks for
                 a strict JSON BUY/SELL/HOLD verdict with a reason.

The final signal is a weighted vote.  The LLM (if configured) is allowed
to override with its own confidence for display; the weighted ensemble is
always shown so the vote stays auditable.
"""

from __future__ import annotations

import json
import os
import threading
import warnings
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from statsmodels.tsa.arima.model import ARIMA
    from arch import arch_model

try:
    import xgboost as xgb

    XGB_OK = True
except Exception:  # noqa: BLE001 - XGBoost is optional for lightweight deployments
    xgb = None
    XGB_OK = False

try:
    import torch
    import torch.nn as nn

    TORCH_OK = True
except Exception:  # noqa: BLE001 - LSTM is optional
    torch = None
    nn = None
    TORCH_OK = False

from strategy import PriceActionStrategy
from trend_reversal import TrendReversalStrategy
from breakout_breakdown import BreakoutBreakdownStrategy

__all__ = ["AISignalEngine", "AISignalError"]


class AISignalError(RuntimeError):
    pass


# ---------------------------------------------------------------- indicators
def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100 - 100 / (1 + rs)


FEATURE_COLS = [
    "ret", "ema9", "ema21", "ema50", "ema169", "slope169", "dist169",
    "rsi", "vol20", "vol_scale", "vol_z", "body", "range", "zscore6",
    "brk_up", "brk_dn", "ret_lag1", "ret_lag2", "ret_lag3", "ret_lag5",
]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    close = d["close"].astype(float)
    vol = d["volume"].astype(float)
    d["ret"] = close.pct_change()
    d["logret"] = np.log(close).diff()
    for n in (9, 21, 50, 169):
        d[f"ema{n}"] = close.ewm(span=n, adjust=False).mean()
    d["slope169"] = d["ema169"] - d["ema169"].shift(1)
    d["dist169"] = (close - d["ema169"]) / d["ema169"].replace(0.0, np.nan)
    d["rsi"] = _rsi(close, 14)
    d["vol20"] = d["ret"].rolling(20).std()
    d["vol_scale"] = d["vol20"] / d["vol20"].rolling(100).mean().replace(0.0, np.nan)
    vol_std = vol.rolling(20).std().replace(0.0, np.nan)
    d["vol_z"] = (vol - vol.rolling(20).mean()) / vol_std
    d["body"] = (close - d["open"]) / (d["high"] - d["low"]).replace(0.0, np.nan)
    d["range"] = (d["high"] - d["low"]) / close.replace(0.0, np.nan)
    sd6 = (close / close.shift(6) - 1).rolling(20).std().replace(0.0, np.nan)
    d["zscore6"] = (close - close.shift(6)) / (sd6 * close)
    d["high5"] = d["high"].rolling(5).max().shift(1)
    d["low5"] = d["low"].rolling(5).min().shift(1)
    d["brk_up"] = (close > d["high5"]).astype(float)
    d["brk_dn"] = (close < d["low5"]).astype(float)
    for lag in (1, 2, 3, 5):
        d[f"ret_lag{lag}"] = d["ret"].shift(lag)
    return d


def _vote_name(vote: float) -> str:
    return {1: "BUY", -1: "SELL", 0: "HOLD"}.get(int(np.sign(vote)), "HOLD")


# ------------------------------------------------------------------ models
def arima_vote(close: pd.Series, window: int = 160) -> dict[str, Any]:
    """ARIMA(1,1,1) on log price. Direction from the 1-step forecast."""
    y = np.log(close.to_numpy(dtype=float))
    y = y[np.isfinite(y)][-window:]
    if len(y) < 40:
        return {"active": False, "note": "insufficient data"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ARIMA(y, order=(1, 1, 1), trend="n").fit(method_kwargs={"maxiter": 60})
        fc = fit.get_forecast(1)
        mean = float(fc.predicted_mean[0])
        std = float(fc.se_mean[0]) if not np.isnan(fc.se_mean[0]) else 0.0
    price = float(y[-1])
    diff = mean - price
    guard = std if std > 1e-12 else abs(diff) or 1e-9
    conf = min(abs(diff) / (4.0 * guard), 1.0)
    return {
        "active": True,
        "vote": 1 if diff > 0 else (-1 if diff < 0 else 0),
        "confidence": conf,
        "forecast": float(np.exp(mean)),
        "price": float(np.exp(price)),
    }


def garch_vote(close: pd.Series, window: int = 300) -> dict[str, Any]:
    """AR(1)-GARCH(1,1) on log returns. Direction from the conditional mean."""
    y = np.log(close.astype(float))
    y = y.to_numpy(dtype=float)
    y = np.diff(y[np.isfinite(y)])[-window:] * 100.0
    if len(y) < 60:
        return {"active": False, "note": "insufficient data"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = arch_model(y, mean="AR", lags=1, vol="GARCH", p=1, q=1, rescale=False)
        res = model.fit(
            disp="off", show_warning=False, update_freq=0,
            options={"maxiter": 200},
        )
        fc = res.forecast(horizon=1, reindex=False)
        mean_fc = float(fc.mean.iloc[-1, 0])
        vol_fc = float(np.sqrt(float(fc.variance.iloc[-1, 0])))
    guard = vol_fc if vol_fc > 1e-9 else 1e-9
    conf = min(abs(mean_fc) / (4.0 * guard), 1.0)
    return {
        "active": True,
        "vote": 1 if mean_fc > 0 else (-1 if mean_fc < 0 else 0),
        "confidence": conf,
        "mean_forecast_pct": mean_fc,
        "vol_forecast_pct": vol_fc,
    }


def kalman_vote(close: pd.Series, window: int = 250) -> dict[str, Any]:
    """Local-linear-trend Kalman filter on log price. Vote = trend slope."""
    y = np.log(close.astype(float)).to_numpy(dtype=float)
    y = y[np.isfinite(y)][-window:]
    n = len(y)
    if n < 40:
        return {"active": False, "note": "insufficient data"}
    r = 1e-2            # measurement noise
    q_level = 1e-4      # level process noise
    q_slope = 1e-7      # slope process noise
    F = np.array([[1.0, 1.0], [0.0, 1.0]])
    H = np.array([1.0, 0.0])
    Q = np.diag([q_level, q_slope])
    P = np.eye(2) * 1.0
    x = np.array([y[0], 0.0])
    for obs in y:
        x_pred = F @ x
        P_pred = F @ P @ F.T + Q
        y_err = obs - H @ x_pred
        S = H @ P_pred @ H + r
        K = P_pred @ H / S
        x = x_pred + K * y_err
        P = (np.eye(2) - np.outer(K, H)) @ P_pred
    slope = float(x[1])
    level = float(x[0])
    step = np.std(np.diff(y)) if len(y) > 1 else 0.0
    guard = step if step > 1e-12 else 1e-9
    conf = min(abs(slope) / (2.0 * guard), 1.0)
    return {
        "active": True,
        "vote": 1 if slope > 0 else (-1 if slope < 0 else 0),
        "confidence": conf,
        "level": float(np.exp(level)),
        "slope": slope,
        "next_price": float(np.exp(level + slope)),
    }


class XGBoostModel:
    """XGBoost classifier (3-class) trained on the actual OHLCV data.

    Retrained lazily when more than ``retrain_every`` new bars arrive.
    """

    def __init__(self, retrain_every: int = 8, min_rows: int = 80) -> None:
        self.retrain_every = retrain_every
        self.min_rows = min_rows
        self._model: Any = None
        self._trained_on = -1
        self._lock = threading.Lock()

    def report(self, df: pd.DataFrame) -> dict[str, Any]:
        if not XGB_OK:
            return {"active": False, "note": "xgboost not installed"}
        if len(df) - self._trained_on >= self.retrain_every:
            self._fit(df)
        if self._model is None:
            return {"active": False, "note": "not enough labelled rows"}
        last = build_features(df).iloc[-1]
        feats = last[FEATURE_COLS].to_numpy(dtype=float).reshape(1, -1)
        feats = np.nan_to_num(feats, nan=0.0, posinf=0.0, neginf=0.0)
        proba = self._model.predict(xgb.DMatrix(feats))[0]
        p_buy, p_sell, p_hold = float(proba[1]), float(proba[0]), float(proba[2])
        vote = 1 if p_buy > max(p_sell, 0.5) else (-1 if p_sell > max(p_buy, 0.5) else 0)
        confidence = max(p_buy, p_sell) if vote != 0 else 0.0
        return {
            "active": True,
            "rows_trained": self._trained_on,
            "vote": vote,
            "confidence": confidence,
            "p_buy": p_buy,
            "p_sell": p_sell,
            "p_hold": p_hold,
        }

    def _fit(self, df: pd.DataFrame) -> None:
        if not XGB_OK:
            return
        with self._lock:
            feats = build_features(df)
            close = df["close"].astype(float).to_numpy()
            y = np.sign(np.roll(close, -1) - close)  # next-bar direction
            X = feats[FEATURE_COLS].to_numpy(dtype=float)
            X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
            mask = np.isfinite(y) & ~np.isnan(y)
            Xt, yt = X[mask], y[mask]
            yt = yt.astype(int) + 1  # classes {0: SELL, 1: BUY, 2: HOLD}
            if len(yt) < self.min_rows:
                self._model = None
                self._trained_on = len(df)
                return
            params = {
                "objective": "multi:softprob",
                "num_class": 3,
                "max_depth": 3,
                "eta": 0.05,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "lambda": 1.0,
                "tree_method": "hist",
                "nthread": 1,
            }
            model = xgb.train(params, xgb.DMatrix(Xt, label=yt), num_boost_round=150)
            self._model = model
            self._trained_on = len(df)


class _LSTMWrapper(nn.Module if TORCH_OK else object):
    def __init__(self, n_features: int, hidden: int = 24):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=n_features, hidden_size=hidden, num_layers=1, batch_first=True
        )
        self.head = nn.Linear(hidden, 3)

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.head(out[:, -1, :])


class LSTMModel:
    """Small single-layer LSTM classifier (label = sign of the next-bar return).

    Trained on the actual OHLCV data with a fixed feature set (same as
    XGBoost). Features are z-scored against the labelled training bars.
    Retrained lazily when more than ``retrain_every`` new bars arrive.
    """

    def __init__(
        self,
        seq_len: int = 28,
        hidden: int = 24,
        epochs: int = 40,
        batch_size: int = 32,
        lr: float = 1e-3,
        retrain_every: int = 8,
        min_rows: int = 140,
    ) -> None:
        self.seq_len = seq_len
        self.hidden = hidden
        self.epochs = epochs
        self.batch_size = batch_size
        self.lr = lr
        self.retrain_every = retrain_every
        self.min_rows = min_rows
        self._model: nn.Module | None = None
        self._trained_on = -1
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._lock = threading.Lock()

    def report(self, df: pd.DataFrame) -> dict[str, Any]:
        if not TORCH_OK:
            return {"active": False, "note": "torch not installed"}
        if len(df) - self._trained_on >= self.retrain_every:
            self._fit(df)
        if self._model is None:
            return {"active": False, "note": "not enough labelled rows"}
        feats = self._features(df)
        last_seq = torch.tensor(
            feats[-self.seq_len :], dtype=torch.float32
        ).unsqueeze(0)
        with torch.no_grad():
            proba = torch.softmax(self._model(last_seq), dim=1)[0].numpy()
        p_sell, p_buy, p_hold = float(proba[0]), float(proba[1]), float(proba[2])
        vote = 1 if p_buy > max(p_sell, 0.5) else (-1 if p_sell > max(p_buy, 0.5) else 0)
        confidence = max(p_buy, p_sell) if vote != 0 else 0.0
        return {
            "active": True,
            "rows_trained": self._trained_on,
            "vote": vote,
            "confidence": confidence,
            "p_buy": p_buy,
            "p_sell": p_sell,
            "p_hold": p_hold,
        }

    def _features(self, df: pd.DataFrame) -> np.ndarray:
        feats = build_features(df)
        X = np.nan_to_num(
            feats[FEATURE_COLS].to_numpy(dtype=float),
            nan=0.0, posinf=0.0, neginf=0.0,
        )
        if self._mean is None or self._std is None:
            return X
        return (X - self._mean) / self._std

    def _fit(self, df: pd.DataFrame) -> None:
        if not TORCH_OK:
            return
        with self._lock:
            torch.set_num_threads(1)
            torch.manual_seed(42)
            X = self._features(df)
            n = len(X)
            close = df["close"].astype(float).to_numpy()
            y = np.full(n, np.nan)
            y[:-1] = np.sign(close[1:] - close[:-1])  # y[i] = direction i -> i+1
            labelled = np.isfinite(y)
            if int(labelled.sum()) < self.min_rows or n < self.seq_len + 10:
                self._model = None
                self._trained_on = len(df)
                return
            self._mean = X[labelled].mean(axis=0)
            self._std = X[labelled].std(axis=0)
            self._std[self._std < 1e-9] = 1.0
            Xz = self._features(df)  # re-standardize with the stored scaler

            seqs, labs = [], []
            for t in range(self.seq_len - 1, n - 1):
                seqs.append(Xz[t - self.seq_len + 1 : t + 1])
                labs.append(int(y[t]) + 1)  # classes {0 sell, 1 buy, 2 hold}
            if len(seqs) < 30:
                self._model = None
                self._trained_on = len(df)
                return

            xs = torch.tensor(np.stack(seqs), dtype=torch.float32)
            ys = torch.tensor(np.array(labs), dtype=torch.long)
            model = _LSTMWrapper(len(FEATURE_COLS), self.hidden)
            opt = torch.optim.Adam(model.parameters(), lr=self.lr, weight_decay=1e-4)
            lossf = nn.CrossEntropyLoss()
            n_seq = len(xs)
            model.train()
            for _ in range(self.epochs):
                perm = torch.randperm(n_seq)
                for i in range(0, n_seq, self.batch_size):
                    idx = perm[i : i + self.batch_size]
                    opt.zero_grad()
                    loss = lossf(model(xs[idx]), ys[idx])
                    loss.backward()
                    opt.step()
            model.eval()
            self._model = model
            self._trained_on = len(df)


# ---------------------------------------------------------------- strategies
_STRATEGIES: list[tuple[str, Any]] = [
    ("Price Action", PriceActionStrategy()),
    ("Trend Reversal", TrendReversalStrategy()),
    ("Breakout / Breakdown", BreakoutBreakdownStrategy()),
]


def _strategy_verdict(name: str, strategy: Any, df: pd.DataFrame) -> dict[str, Any]:
    try:
        sig = str(strategy.generate_signal(df).iloc[-1]["signal"])
    except Exception as exc:  # noqa: BLE001 - keep the engine alive
        return {"active": False, "signal": "HOLD", "note": str(exc)}
    return {"active": True, "signal": sig, "vote": {"BUY": 1, "SELL": -1}.get(sig, 0)}


# ------------------------------------------------------------------ LLM judge
LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "label": "Ollama (local, open-source)",
        "style": "openai",
        "base": "http://localhost:11434/v1",
        "key_env": None,
        "default_model": "llama3.1:8b",
        "models": [
            "llama3.1:8b", "llama3.2:3b", "mistral:7b",
            "qwen2.5:7b", "phi4:14b", "deepseek-r1:7b",
        ],
    },
    "huggingface": {
        "label": "HuggingFace (open-source)",
        "style": "openai",
        "base": "https://router.huggingface.co/v1",
        "key_env": "HF_API_TOKEN",
        "default_model": "meta-llama/Llama-3.1-8B-Instruct",
        "models": [
            "meta-llama/Llama-3.1-8B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.2-3B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
    },
    "openrouter": {
        "label": "OpenRouter (aggregator)",
        "style": "openai",
        "base": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "openai/gpt-4o-mini",
        "models": [
            "openai/gpt-4o-mini", "openai/gpt-4o",
            "anthropic/claude-3.5-sonnet",
            "meta-llama/llama-3.3-70b-instruct",
            "deepseek/deepseek-chat",
            "google/gemini-flash-1.5",
        ],
    },
    "groq": {
        "label": "Groq (open-source, fast)",
        "style": "openai",
        "base": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "models": [
            "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
            "llama3-70b-8192", "mixtral-8x7b-32768", "gemma2-9b-it",
        ],
    },
    "claude": {
        "label": "Claude (Anthropic, paid)",
        "style": "anthropic",
        "base": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-5",
        "models": [
            "claude-sonnet-4-5",
            "claude-3-7-sonnet-20250219",
            "claude-sonnet-4-0",
            "claude-opus-4-1",
        ],
    },
    "chatgpt": {
        "label": "ChatGPT (OpenAI, paid)",
        "style": "openai",
        "base": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
    },
}


def list_llm_providers() -> list[str]:
    return list(LLM_PROVIDERS)


def llm_models_for(provider: str) -> list[str]:
    return list((LLM_PROVIDERS.get(provider or "") or {}).get("models", []))


def llm_default_model(provider: str) -> str:
    return str((LLM_PROVIDERS.get(provider or "") or {}).get("default_model", ""))


def _extract_json(text: str, default: dict) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - try raw JSON slice
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except Exception:  # noqa: BLE001
                pass
    return default


class LLMJudge:
    """Provider-aware LLM verdict.

    Supports local/open-source providers (Ollama, HuggingFace, OpenRouter,
    Groq) and paid ones (Anthropic Claude, OpenAI ChatGPT). ``provider`` and
    ``model`` can be passed in (from the UI dropdowns) or read from
    ``LLM_PROVIDER`` / ``LLM_MODEL``. The base URL comes from the provider
    registry unless ``LLM_BASE_URL`` is set; the API key from ``LLM_API_KEY``
    or the provider's own env var (see below).
    """

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.timeout = float(os.environ.get("LLM_TIMEOUT", "45"))
        provider = (
            (provider or "").strip().lower()
            or (os.environ.get("LLM_PROVIDER") or "").strip().lower()
        )
        cfg = LLM_PROVIDERS.get(provider) or {}
        self.provider = provider or "custom"
        self.base_url = (
            (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/")
            or str(cfg.get("base", ""))
        )
        key_env = cfg.get("key_env")
        self._key_env = str(key_env) if key_env else None
        self.api_key = (api_key or os.environ.get("LLM_API_KEY") or "").strip()
        if not self.api_key and self._key_env:
            self.api_key = (os.environ.get(self._key_env) or "").strip()
        self.style = str(cfg.get("style", "openai")) or "openai"
        self.model = (
            (model or "").strip()
            or (os.environ.get("LLM_MODEL") or "").strip()
            or str(cfg.get("default_model", ""))
        )
        self.enabled = bool(
            self.base_url and self.model and self.base_url.startswith(("http://", "https://"))
        )

    def label(self) -> str:
        return f"{self.provider} / {self.model}" if self.model else self.provider

    def judge(
        self,
        context: dict[str, Any],
        signal: str,
        confidence: float,
        reason: str,
    ) -> dict[str, Any]:
        if not self.base_url:
            return self._reply(None, "no provider configured (pick a provider/model above)")
        if not self.base_url.startswith(("http://", "https://")):
            return self._reply(None, f"invalid base URL: {self.base_url}")
        if not self.model:
            return self._reply(None, "no model selected")
        if self._key_env and not self.api_key:
            return self._reply(None, f"missing {self._key_env} in .env for {self.provider}")
        prompt = (
            "You are a quantitative trading analyst. Given the following live 5-minute "
            "market analysis, output a STRICT single JSON object with keys "
            '"signal" (\'BUY\' | \'SELL\' | \'HOLD\'), "confidence" (0..1 float) and '
            '"reason" (<=20 words). No markdown, no extra text.\n\n'
            + json.dumps(context, default=str)[:6000]
        )
        try:
            import httpx  # noqa: F401
        except Exception:  # noqa: BLE001
            return self._reply(None, "httpx not installed (pip install httpx)")
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if self.style == "anthropic":
                    resp = client.post(
                        f"{self.base_url}/messages",
                        headers={
                            "Content-Type": "application/json",
                            "x-api-key": self.api_key,
                            "anthropic-version": "2023-06-01",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": 512,
                            "temperature": 0.2,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                else:
                    payload = {
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "You return only compact JSON."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    }
                    headers = {"Content-Type": "application/json"}
                    if self.api_key:
                        headers["Authorization"] = f"Bearer {self.api_key}"
                    resp = client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=payload
                    )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:  # noqa: BLE001 - surfaced nicely
            return self._reply(None, self._http_error(exc))
        except Exception as exc:  # noqa: BLE001 - LLM failures must not crash
            return self._reply(None, f"LLM error: {exc}")
        try:
            data = resp.json()
            if self.style == "anthropic":
                content = data["content"][0]["text"]
            else:
                content = data["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001 - bad response shape
            return self._reply(None, "unparseable LLM response")
        parsed = _extract_json(
            content,
            {"signal": signal, "confidence": confidence, "reason": "unparseable LLM reply"},
        )
        llm_signal = str(parsed.get("signal", "HOLD")).upper()
        if llm_signal not in ("BUY", "SELL", "HOLD"):
            llm_signal = "HOLD"
        return self._reply(
            llm_signal,
            str(parsed.get("reason", "")),
            float(parsed.get("confidence", 0.5)),
        )

    def _http_error(self, exc) -> str:
        code = getattr(exc, "response", None)
        status = int(getattr(code, "status_code", 0) or 0)
        body = ""
        try:
            body = (code.text or "").strip()
        except Exception:  # noqa: BLE001
            body = ""
        low = (body or "").lower()
        if status == 404 and "not found" in low and self.provider == "ollama":
            return (
                f"Ollama model is not installed - download it once: "
                f"`ollama pull {self.model}` (or pick one already in `ollama list`)"
            )
        if status == 404:
            return f"HTTP 404 from {self.provider} - endpoint or model not found"
        if status in (401, 403):
            return f"authentication failed for {self.provider} - check your API key in .env"
        if status == 429:
            return "rate limited (429) - try again in a few seconds"
        if status and body and len(body) < 400:
            return f"HTTP {status}: {body[:300]}"
        return f"HTTP error {status}" if status else str(exc)

    def _reply(self, llm_signal: str | None, reason: str, confidence: float = 0.0) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "label": self.label(),
            "reason": str(reason)[:300],
        }
        if llm_signal:
            out["signal"] = llm_signal
            out["confidence"] = float(confidence)
        return out


# ---------------------------------------------------------------- the engine
class AISignalEngine:
    """Runs the model stack on the latest closed candle and returns a verdict."""

    def __init__(
        self,
        use_arima: bool = True,
        use_garch: bool = True,
        use_kalman: bool = True,
        use_xgb: bool = True,
        use_lstm: bool = True,
        use_strategies: bool = True,
        min_bars: int = 80,
        buy_threshold: float = 0.22,
        sell_threshold: float = -0.22,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.use_arima = use_arima
        self.use_garch = use_garch
        self.use_kalman = use_kalman
        self.use_xgb = use_xgb
        self.use_lstm = use_lstm
        self.use_strategies = use_strategies
        self.min_bars = min_bars
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.weights = weights or {
            "arima": 1.0, "garch": 0.7, "kalman": 1.0, "xgb": 1.2, "lstm": 1.2,
            "Price Action": 0.5, "Trend Reversal": 0.6, "Breakout / Breakdown": 0.6,
        }
        self.xgb_model = XGBoostModel()
        self.lstm_model = LSTMModel()
        self.llm = LLMJudge()
        self.last_fit_len = -1

    # ------------------------------------------------------------ internals
    def _models(self, df: pd.DataFrame) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.use_arima:
            out["arima"] = arima_vote(df["close"])
        if self.use_garch:
            out["garch"] = garch_vote(df["close"])
        if self.use_kalman:
            out["kalman"] = kalman_vote(df["close"])
        if self.use_xgb:
            out["xgboost"] = self.xgb_model.report(df)
        if self.use_lstm:
            out["lstm"] = self.lstm_model.report(df)
        return out

    def _strategies(self, df: pd.DataFrame) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not self.use_strategies:
            return out
        for name, strat in _STRATEGIES:
            out[name] = _strategy_verdict(name, strat, df)
        return out

    @staticmethod
    def _score(votes: dict[str, dict[str, Any]], weights: dict[str, float]) -> float:
        total = 0.0
        max_score = 0.0
        for name, model in votes.items():
            if not model.get("active"):
                continue
            w = weights.get(name, 0.0)
            total += w * float(model.get("vote", 0))
            max_score += w
        if max_score <= 0:
            return 0.0
        return total / max_score

    def set_llm(
        self,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Swap the LLM judge to a given provider/model (UI dropdown selection)."""
        self.llm = LLMJudge(provider=provider, model=model, api_key=api_key)

    # -------------------------------------------------------------- entry point
    def analyze(self, df: pd.DataFrame) -> dict[str, Any]:
        df = df.copy().sort_values("timestamp").reset_index(drop=True)
        if len(df) < self.min_bars:
            raise AISignalError(
                f"warming up - need >= {self.min_bars} closed bars, have {len(df)}"
            )
        last = df.iloc[-1]
        f = lambda x: float(x) if pd.notna(x) else float("nan")  # noqa: E731

        models = self._models(df)
        strategies = self._strategies(df)
        votes: dict[str, dict[str, Any]] = {**strategies, **models}
        score = self._score(votes, self.weights)
        signal = (
            "BUY" if score >= self.buy_threshold
            else "SELL" if score <= self.sell_threshold
            else "HOLD"
        )
        confidence = min(abs(score) / max(abs(self.buy_threshold) * 2.0, 1e-9), 1.0)

        feats = build_features(df).iloc[-1]
        indicators = {
            "price": f(feats.get("close", last["close"])),
            "rsi": f(feats.get("rsi")),
            "ema169": f(feats.get("ema169")),
            "ema21": f(feats.get("ema21")),
            "ema9": f(feats.get("ema9")),
            "close_vs_ema169_pct": f(feats.get("dist169") * 100.0),
            "slope169": f(feats.get("slope169")),
            "vol_z": f(feats.get("vol_z")),
            "vol20_pct": f(feats.get("vol20") * 100.0),
        }

        reasons = []
        for name, m in votes.items():
            vote = m.get("vote")
            if vote not in (1, -1):
                continue
            reasons.append(f"{name}:{_vote_name(vote)}")
        reason = "score={score:+.2f} [{reasons}]".format(
            score=score, reasons=", ".join(reasons) or "no votes"
        )

        context = {
            "timestamp": str(last["timestamp"]),
            "candle": {c: f(last[c]) for c in ("open", "high", "low", "close", "volume")},
            "indicators": indicators,
            "models": {
                k: {kk: vv for kk, vv in m.items() if kk != "active"}
                for k, m in models.items()
            },
            "strategies": {
                k: m.get("signal") for k, m in strategies.items()
            },
            "ensemble": {"signal": signal, "score": round(score, 3), "confidence": round(confidence, 3)},
        }

        llm = self.llm.judge(context, signal, confidence, reason)
        llm_signal = llm.get("signal") if llm else None

        return {
            "timestamp": str(last["timestamp"]),
            "price": last["close"],
            "signal": signal,
            "score": score,
            "confidence": confidence,
            "reason": reason,
            "indicators": indicators,
            "models": models,
            "strategies": strategies,
            "llm": llm,
            "llm_signal": llm_signal or (signal if llm else signal),
            "llm_enabled": bool(llm and llm.get("signal")),
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        }