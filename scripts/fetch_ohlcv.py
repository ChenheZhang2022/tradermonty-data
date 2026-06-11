#!/usr/bin/env python3
"""Fetch + CROSS-VALIDATE daily OHLCV for the evening routine.

Primary data: yfinance QQQ + ^VIX (adjusted, 3y). Independent reference:
FMP SPY + ^VIX (free tier; QQQ/^NDX not available there). Validation
compares the two sources on overlapping symbols and checks QQQ/SPY
coherence; result goes into latest/ohlcv_status.json as
{"degraded": bool, "validation": {...}}. The CCR routine's iron rule:
degraded=true -> no DEFENSE-trigger declarations, no DCA-pause/close
instructions (incident 2026-06-11: unvalidated scraped MA200 produced a
false trigger report).
"""
import os, json, datetime
import pandas as pd

def yf_fetch(sym, adjust):
    import yfinance as yf
    df = yf.download(sym, period="3y", auto_adjust=adjust, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])  # yfinance may emit a partial last row
    if len(df) < 300:
        raise RuntimeError(f"{sym}: only {len(df)} rows")
    return df[["Open", "High", "Low", "Close", "Volume"]]

def fmp_fetch(sym):
    import requests
    key = os.environ["FMP_API_KEY"]
    frm = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    r = requests.get(
        "https://financialmodelingprep.com/stable/historical-price-eod/full",
        params={"symbol": sym, "from": frm, "apikey": key}, timeout=30)
    rows = r.json()
    if not isinstance(rows, list) or len(rows) < 10:
        raise RuntimeError(f"FMP {sym}: bad response {str(rows)[:80]}")
    df = pd.DataFrame(rows)
    df.columns = [c.title() for c in df.columns]
    return df.set_index("Date").sort_index()["Close"].astype(float)

status = {"fetched_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
          "source": "yfinance", "degraded": False, "validation": {}, "errors": []}
val = status["validation"]
try:
    def merge_save(df, path):
        # Union with existing rows (TV local push may be fresher than
        # yfinance-on-CI); existing rows win on date conflict.
        try:
            old = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            df.index = pd.to_datetime(df.index).tz_localize(None)
            combined = pd.concat([df[~df.index.isin(old.index)], old]).sort_index()
        except FileNotFoundError:
            combined = df
        combined.to_csv(path)
        return combined

    qqq = merge_save(yf_fetch("QQQ", adjust=True), "latest/ohlcv_qqq.csv")
    vix = merge_save(yf_fetch("^VIX", adjust=True), "latest/ohlcv_vix.csv")
    status["qqq"] = {"rows": len(qqq), "last_date": str(qqq.index[-1])[:10]}
    status["vix"] = {"rows": len(vix), "last_date": str(vix.index[-1])[:10]}

    # ── cross-validation vs FMP (independent feed) ───────────────────
    try:
        spy_yf = yf_fetch("SPY", adjust=False)["Close"]
        spy_yf.index = spy_yf.index.strftime("%Y-%m-%d")
        spy_fmp = fmp_fetch("SPY")
        common = spy_yf.index.intersection(spy_fmp.index)[-10:]
        spy_diff = float((spy_yf[common] / spy_fmp[common] - 1).abs().max())
        vix_yf = vix["Close"].copy(); vix_yf.index = vix_yf.index.strftime("%Y-%m-%d")
        vix_fmp = fmp_fetch("^VIX")
        vcommon = vix_yf.index.intersection(vix_fmp.index)[-10:]
        vix_diff = float((vix_yf[vcommon] / vix_fmp[vcommon] - 1).abs().max())
        qqq_ret = qqq["Close"].pct_change().tail(10)
        spy_ret = yf_fetch("SPY", adjust=True)["Close"].pct_change().tail(10)
        corr = float(qqq_ret.corr(spy_ret))
        gap_days = (pd.Timestamp(spy_fmp.index[-1]) - qqq.index[-1].tz_localize(None)).days
        val.update({"spy_max_diff_pct": round(spy_diff * 100, 3),
                    "vix_max_diff_pct": round(vix_diff * 100, 3),
                    "qqq_spy_ret_corr_10d": round(corr, 3),
                    "fmp_vs_yf_date_gap_days": gap_days})
        val["passed"] = bool(spy_diff < 0.007 and vix_diff < 0.02 and corr > 0.5 and gap_days <= 3)
        if not val["passed"]:
            status["degraded"] = True
            status["errors"].append("cross-validation failed; treat QQQ indicators as unverified")
    except Exception as e:
        val["passed"] = None
        status["errors"].append(f"validation unavailable: {str(e)[:150]}")
except Exception as e:
    status["errors"].append(f"yfinance failed: {str(e)[:150]}")
    status["source"] = "fmp_fallback_no_qqq"
    status["degraded"] = True
    try:
        import requests
        gspc = fmp_fetch("^GSPC"); vixf = fmp_fetch("^VIX")
        gspc.to_frame("Close").to_csv("latest/ohlcv_gspc_fallback.csv")
        vixf.to_frame("Close").to_csv("latest/ohlcv_vix.csv")
    except Exception as e2:
        status["errors"].append(f"fmp fallback failed too: {str(e2)[:150]}")

with open("latest/ohlcv_status.json", "w") as f:
    json.dump(status, f, indent=1)
print(json.dumps(status))
