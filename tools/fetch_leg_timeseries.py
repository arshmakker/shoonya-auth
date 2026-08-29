#!/usr/bin/env python3
"""Pull 1-minute candles for the IC legs we actually traded, via broker_proxy.

Why: nothing persists the position legs' prices. tick_store is in-memory, the
WS subscribe scripts only log subscription bookkeeping, and the market_data
collector only ever captured near-ATM strikes — never the traded wings. So there
is no P&L path for any live trade, which is what calibrating
IC_PROFIT_TRAIL_CONFIRM_SECONDS needs.

`get_time_price_series` is a real NorenApi method, so it dispatches through the
proxy's generic /call route. This asks it for the exact leg contracts.

    # 1. does the history still exist? (one leg, oldest trade) — answer in ~seconds
    python3 tools/fetch_leg_timeseries.py --probe

    # 2. if it does, pull every leg of every trade into CSVs
    python3 tools/fetch_leg_timeseries.py --out ~/leg_series

Run it on the droplet, against the local proxy. The Shoonya session is bound to
the registered IP, so a Mac-side session risks INVALID_IP.

KNOWN LIMITATION: candles are TRADED prices. actionable_pnl marks every leg at
its unfavourable side, so the bid/ask-widening half of the noise is invisible
here. This measures the price-driven component only, at 1-minute resolution —
enough to tell whether adverse excursions die inside a minute or run for five,
not enough to separate 60s from 90s.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PROXY = "http://127.0.0.1:7890"
LOG_DIR = Path.home() / "git/trading/regimetrader/logs"

# Exact tradingsymbols come from the live order lines — no symbol reconstruction.
FILL_RE = re.compile(r"LIVE ORDER (?:BUY|SELL) NFO\|(\S+) status=COMPLETE")
ENTER_RE = re.compile(r"^(\S+ \S+),\d+ .*IC \w+ ENTERED")
EXIT_RE = re.compile(r"^(\S+ \S+),\d+ .*IC \w+ EXIT \[")


def call(proxy: str, method: str, **kwargs):
    body = json.dumps({"method": method, "kwargs": kwargs}).encode()
    req = urllib.request.Request(
        f"{proxy.rstrip('/')}/call", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def resolve_token(proxy: str, symbol: str) -> str | None:
    res = call(proxy, "searchscrip", exchange="NFO", searchtext=symbol)
    for v in (res or {}).get("values", []):
        if v.get("tsym") == symbol:
            return v.get("token")
    return None


def scan_logs() -> dict[str, dict]:
    """{date: {"symbols": [...], "start": dt, "end": dt}} from the live order logs."""
    out: dict[str, dict] = {}
    for f in sorted(LOG_DIR.glob("ic_system_2026*.log")):
        day = f.stem.split("_")[-1]
        syms, times = [], []
        for line in f.open(errors="ignore"):
            m = FILL_RE.search(line)
            if m and m.group(1) not in syms:
                syms.append(m.group(1))
            for pat in (ENTER_RE, EXIT_RE):
                t = pat.match(line)
                if t:
                    times.append(dt.datetime.strptime(t.group(1), "%Y-%m-%d %H:%M:%S"))
        if syms:
            # Always pull the whole session, not just the ENTER..EXIT span: on a
            # day with only an entry those two timestamps are identical, giving a
            # zero-width window, and we want the surrounding context anyway.
            d = dt.datetime.strptime(day, "%Y%m%d")
            out[day] = {
                "symbols": syms,
                "start": d.replace(hour=3, minute=40),   # 09:10 IST, logs are UTC
                "end": d.replace(hour=10, minute=5),     # 15:35 IST
                "events": sorted(times),
            }
    return out


def fetch(proxy: str, token: str, start: dt.datetime, end: dt.datetime, interval: str = "1"):
    return call(
        proxy,
        "get_time_price_series",
        exchange="NFO",
        token=token,
        starttime=str(int(start.timestamp())),
        endtime=str(int(end.timestamp())),
        interval=interval,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--probe", action="store_true", help="retention check only: one leg, one day")
    ap.add_argument("--day", help="YYYYMMDD to probe/pull (default: all; probe uses the oldest). "
                                  "Expired contracts drop off the symbol master, so probe a day "
                                  "whose expiry is still live.")
    ap.add_argument("--out", default="./leg_series", help="directory for the CSVs")
    ap.add_argument("--interval", default="1", help="candle interval in minutes (default 1)")
    args = ap.parse_args()

    try:
        days = scan_logs()
    except OSError as e:
        return int(bool(sys.stderr.write(f"cannot read logs: {e}\n")))
    if not days:
        sys.stderr.write(f"no live fills found in {LOG_DIR}\n")
        return 1

    print(f"Found live fills on {len(days)} day(s): {', '.join(sorted(days))}\n")

    if args.day:
        days = {k: v for k, v in days.items() if k == args.day}
        if not days:
            sys.stderr.write(f"no fills logged on {args.day}\n")
            return 1

    if args.probe:
        day = sorted(days)[0]
        info = days[day]
        sym = info["symbols"][0]
        print(f"RETENTION PROBE — oldest day {day}, leg {sym}")
        token = resolve_token(args.proxy, sym)
        if not token:
            print(f"  could not resolve a token for {sym} (contract may have expired off the master)")
            return 2
        print(f"  token={token}  window {info['start']} → {info['end']}")
        res = fetch(args.proxy, token, info["start"], info["end"], args.interval)
        if isinstance(res, list) and res:
            print(f"  ✓ {len(res)} candles returned. Sample: {json.dumps(res[0])[:200]}")
            print("\n  History exists — a full pull is worth doing.")
            return 0
        print(f"  ✗ no candles: {json.dumps(res)[:300] if res else res}")
        print("\n  Either retention has expired or the contract is gone. Instrumenting forward")
        print("  (logging actionable_pnl each cycle) is then the only path.")
        return 2

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    total = 0
    for day in sorted(days):
        info = days[day]
        for sym in info["symbols"]:
            token = resolve_token(args.proxy, sym)
            if not token:
                print(f"  {day} {sym:28} — no token, skipped")
                continue
            res = fetch(args.proxy, token, info["start"], info["end"], args.interval)
            if not isinstance(res, list) or not res:
                print(f"  {day} {sym:28} — no candles")
                continue
            dst = out / f"{sym}_{day}.csv"
            keys = sorted({k for row in res for k in row})
            with dst.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=keys)
                w.writeheader()
                w.writerows(res)
            total += len(res)
            print(f"  {day} {sym:28} — {len(res):>4} candles → {dst.name}")
    print(f"\n{total} candles written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
