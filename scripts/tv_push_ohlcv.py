#!/usr/bin/env python3
"""Local bridge: push fresh OHLCV from TradingView Desktop to the mirror.

Runs on Chenhe's Mac (launchd, weekdays 17:45 ET, after close). Uses the
tradingview-mcp CLI (CDP bridge). TV data is fresher than yfinance-on-CI
(observed 2026-06-11: TV had the 6/10 bar 8h before yfinance). Merges by
date with the existing CSV; GitHub Action's yfinance fetch stays as
fallback and also merges, so freshest source wins per-date.

Requires TradingView Desktop running with --remote-debugging-port=9222.
Exits quietly if the bridge is down (GH fallback covers the day).
Side effect: flips the active chart to QQQ/VIX 1D briefly, then restores.
"""
import json, subprocess, sys, datetime, os

TV = ["node", os.path.expanduser("~/tradingview-mcp/src/cli/index.js")]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def tv(*args, timeout=45):
    r = subprocess.run(TV + list(args), capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"success": False, "raw": r.stdout[:200], "err": r.stderr[:200]}

def fetch_symbol(sym):
    if not tv("symbol", sym).get("success"):
        raise RuntimeError(f"set symbol {sym} failed")
    tv("timeframe", "1D")
    import time; time.sleep(4)
    res = tv("ohlcv", "-n", "500")
    if not res.get("success") or not res.get("bars"):
        raise RuntimeError(f"ohlcv {sym} failed: {str(res)[:150]}")
    rows = []
    for b in res["bars"]:
        d = datetime.datetime.fromtimestamp(b["time"], datetime.UTC).strftime("%Y-%m-%d")
        rows.append((d, b["open"], b["high"], b["low"], b["close"], b.get("volume", 0)))
    return rows

def merge_csv(path, rows):
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            header = f.readline()
            for line in f:
                parts = line.strip().split(",")
                if parts and parts[0]:
                    existing[parts[0]] = line.strip()
    added = 0
    for d, o, h, l, cl, vol in rows:
        line = f"{d},{o},{h},{l},{cl},{int(vol)}"
        if d not in existing:
            added += 1
        existing[d] = line  # TV wins on conflict (fresher final values)
    with open(path, "w") as f:
        f.write("Date,Open,High,Low,Close,Volume\n")
        for d in sorted(existing):
            f.write(existing[d] + "\n")
    return added, max(existing) if existing else None

def main():
    st = tv("status")
    if not st.get("success") or not st.get("cdp_connected"):
        print("TV bridge down; skipping (GH yfinance fallback covers today)")
        return 0
    subprocess.run(["git", "-C", REPO, "pull", "-q", "--rebase", "--autostash",
                    "origin", "main"], check=True)
    orig = tv("state").get("symbol")
    summary = {"pushed_at_utc": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
               "source": "tradingview_local"}
    try:
        for sym, name in [("QQQ", "qqq"), ("VIX", "vix")]:
            try:
                rows = fetch_symbol(sym)
                added, last = merge_csv(os.path.join(REPO, f"latest/ohlcv_{name}.csv"), rows)
                summary[name] = {"added": added, "last_date": last, "bars": len(rows)}
            except Exception as e:
                summary[name] = {"error": str(e)[:150]}
    finally:
        if orig:
            tv("symbol", orig)
    with open(os.path.join(REPO, "latest/tv_push_status.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary))
    subprocess.run(["git", "-C", REPO, "add", "latest/"], check=True)
    diff = subprocess.run(["git", "-C", REPO, "diff", "--cached", "--quiet"])
    if diff.returncode != 0:
        subprocess.run(["git", "-C", REPO, "commit", "-q", "-m",
                        f"data: TV local push {summary['pushed_at_utc'][:10]}"], check=True)
        subprocess.run(["git", "-C", REPO, "push", "-q", "origin", "main"], check=True)
        print("pushed to mirror")
    else:
        print("no new data")
    return 0

if __name__ == "__main__":
    sys.exit(main())
