"""Live test: Alpha Vantage for commodities (gold/silver/crude oil) and currency pairs.

Runs the real AlphaVantageLiveEngine (without starting threads) and also checks the
raw Alpha Vantage endpoints so we can see exactly what works on the current key.
"""
from __future__ import annotations

import os

import dotenv

dotenv.load_dotenv()

import httpx

from alphavantage_feed import _resolve_av_symbol, AlphaVantageLiveEngine

KEY = os.environ["ALPHAVANTAGE_API_KEY"]
BASE = "https://www.alphavantage.co/query"
http = httpx.Client(timeout=15.0)


def raw(function: str, **params) -> dict:
    r = http.get(BASE, params={"function": function, "apikey": KEY, **params})
    return r.json()


def show(label: str, data: dict, price_keys: list[tuple]) -> None:
    print(f"\n=== {label} ===")
    if "Information" in data:
        print("  Information:", str(data["Information"])[:200])
        return
    if "Error Message" in data:
        print("  Error:", str(data["Error Message"])[:200])
        return
    for wrapper, key in price_keys:
        node = data.get(wrapper) if wrapper else data
        if isinstance(node, dict) and key in node:
            print(f"  OK  {wrapper or 'root'} -> {key} = {node[key]}")
            return
    print("  Unexpected payload:", str(data)[:200])


# ---- 1) Symbol resolution ----------------------------------------------------
print("== Symbol resolution ==")
for sym in ["GOLD", "XAUUSD", "SILVER", "XAG", "EURUSD", "USDINR", "CL=F", "WTI", "BRENT", "CRUDE_OIL"]:
    try:
        r = _resolve_av_symbol(sym)
        print(f"  {sym:10s} -> {r.get('instrument_type'):10s} {r.get('display')}")
    except Exception as exc:
        print(f"  {sym:10s} -> ERROR: {exc}")

# ---- 2) Raw endpoints -------------------------------------------------------
# Gold & silver spot (free, no wrapper)
data = raw("GOLD_SILVER_SPOT", symbol="GOLD")
show("GOLD_SILVER_SPOT GOLD", data, [("", "price")])
data = raw("GOLD_SILVER_SPOT", symbol="SILVER")
show("GOLD_SILVER_SPOT SILVER", data, [("", "price")])

# Forex
data = raw("CURRENCY_EXCHANGE_RATE", from_currency="EUR", to_currency="USD")
show(
    "CURRENCY_EXCHANGE_RATE EUR/USD",
    data,
    [("Realtime Currency Exchange Rate", "5. Exchange Rate")],
)
data = raw("CURRENCY_EXCHANGE_RATE", from_currency="USD", to_currency="INR")
show(
    "CURRENCY_EXCHANGE_RATE USD/INR",
    data,
    [("Realtime Currency Exchange Rate", "5. Exchange Rate")],
)

# Crude oil - Alpha Vantage free commodities endpoint (daily, not realtime)
data = raw("COMMODITIES", interval="daily", commodity="brent_crude_oil")
show(
    "COMMODITIES brent_crude_oil (daily)",
    data,
    [("data", "value")],
)
data = raw("COMMODITIES", interval="daily", commodity="wti_crude_oil")
show(
    "COMMODITIES wti_crude_oil (daily)",
    data,
    [("data", "value")],
)

# Full WTI payload sample (first entry only)
data = raw("COMMODITIES", interval="daily", commodity="wti_crude_oil")
if isinstance(data.get("data"), list) and data["data"]:
    print("\n  WTI latest:", data["data"][0])

http.close()