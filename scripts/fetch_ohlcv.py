#!/usr/bin/env python3
"""Fetch daily OHLCV for the trading system's evening routine.

Primary: yfinance QQQ + ^VIX (3y daily). Fallback: FMP ^GSPC + ^VIX
(free tier does not cover QQQ/^NDX). Always writes latest/ohlcv_status.json
so downstream consumers can detect source and freshness. The CCR routine
must compute MA200/RSI/etc from these CSVs deterministically — never from
scraped web values (incident 2026-06-11: scraped MA200 caused a false
DEFENSE-trigger report).
"""
import os, json, datetime
import pandas as pd

def via_yf():
    import yfinance as yf
    out = {}
    for sym, name in [("QQQ", "qqq"), ("^VIX", "vix")]:
        df = yf.download(sym, period="3y", auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])  # yfinance may emit a partial last row
        if len(df) < 300:
            raise RuntimeError(f"{sym}: only {len(df)} rows")
        out[name] = df[["Open", "High", "Low", "Close", "Volume"]]
    return out, "yfinance"

def via_fmp():
    import requests
    key = os.environ["FMP_API_KEY"]
    frm = (datetime.date.today() - datetime.timedelta(days=1100)).isoformat()
    out = {}
    for sym, name in [("^GSPC", "gspc_fallback"), ("^VIX", "vix")]:
        r = requests.get(
            "https://financialmodelingprep.com/stable/historical-price-eod/full",
            params={"symbol": sym, "from": frm, "apikey": key}, timeout=30)
        rows = r.json()
        if not isinstance(rows, list) or len(rows) < 300:
            raise RuntimeError(f"{sym}: bad response {str(rows)[:80]}")
        df = pd.DataFrame(rows)
        df.columns = [c.title() for c in df.columns]
        df = df.set_index("Date").sort_index()
        out[name] = df[["Open", "High", "Low", "Close", "Volume"]]
    return out, "fmp_fallback_no_qqq"

status = {"fetched_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
          "source": None, "yf_error": None}
try:
    data, src = via_yf()
except Exception as e:
    status["yf_error"] = str(e)[:200]
    data, src = via_fmp()
status["source"] = src
for name, df in data.items():
    df.to_csv(f"latest/ohlcv_{name}.csv")
    status[name] = {"rows": len(df), "last_date": str(df.index[-1])[:10]}
with open("latest/ohlcv_status.json", "w") as f:
    json.dump(status, f, indent=1)
print(json.dumps(status))
