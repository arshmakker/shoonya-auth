#!/usr/bin/env python
"""leg_tick_collector.py — persist per-leg bid/ask for the OPEN position's legs.

The gap this closes: MCX has `regimetrader/tools/mcx_collector.py` writing
per-contract CSVs with bid/ask at ~22s, but the NIFTY option legs we actually
trade have nothing. `market_data`'s option collector only captures strikes near
spot (24150-24550 over 24-28 Aug 2026) and never the traded wings
(23150-25200); `tick_store` is an in-memory dict; the ws_subscribe_* scripts log
only subscription bookkeeping. So no live position has a leg-level price record.

That cost real work: with no historical bid/ask, `actionable_pnl` cannot be
replayed. Reconstructing from `get_time_price_series` gives TRADED prices, while
actionable_pnl marks every leg at its UNFAVOURABLE side — a bias that flatters
tight floors and penalises patience, so a 1,205-path backtest built on it could
not settle any floor or timer question.

Cost of fixing it: zero extra broker calls. ws_subscribe_chain.py already
subscribes the open position's own legs (commit 701e536), so their touchlines
are already arriving and being cached. This reads broker_proxy's /tick endpoint
— the local WS cache — and appends CSVs. Because nothing hits the broker,
cadence is effectively free and can be far finer than the 139s the market_data
option collector managed or the 1-minute floor on get_time_price_series.

    ./venv/bin/python tools/leg_tick_collector.py

Writes regimetrader/market_data_YYYYMMDD/raw_data/option_legs/<SYMBOL>_<day>.csv,
mirroring mcx_collector's column shape so downstream parsing is the same.

Every poll is written, including unchanged ticks: regular sampling is what makes
the series usable as a time series. The feed's own `ts` is recorded alongside
the local poll time so a stale cache entry is visible rather than silently
looking like a fresh quote.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time

from proxy_client import get, resolve
from ws_subscribe_chain import position_leg_symbols

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DEFAULT_POSITIONS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "regimetrader", "data", "open_positions.json"
)
DEFAULT_OUT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "regimetrader")

# NSE F&O runs 09:15-15:30 IST; bracket it so nothing is clipped at either end.
SESSION_START = dt.time(9, 10)
SESSION_END = dt.time(15, 35)

POLL_SEC = 5.0
# Re-read open_positions.json this often: a roll or a fresh entry changes the
# leg set mid-session, and a collector pinned to the boot-time legs would then
# be recording contracts the system no longer holds.
REFRESH_SEC = 120.0

COLUMNS = (
    "poll_time",   # when we read the cache
    "tick_time",   # the feed's own ts — compare against poll_time to see staleness
    "symbol",
    "token",
    "ltp",
    "bid",
    "ask",
    "bid_qty",
    "ask_qty",
    "oi",
    "volume",
)


def out_path(root: str, symbol: str, day: str) -> str:
    d = os.path.join(root, f"market_data_{day}", "raw_data", "option_legs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}_{day}.csv")


def append_row(path: str, row: dict) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def resolve_legs(positions_file: str) -> dict[str, str]:
    """{bare symbol: 'NFO|<token>'} for the open position's legs, if any."""
    out = {}
    for raw in position_leg_symbols(positions_file):
        sym = raw.split("|", 1)[1] if "|" in raw else raw
        spec = resolve(sym)
        if spec:
            out[sym] = spec
    return out


def in_session(now: dt.datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions-file", default=DEFAULT_POSITIONS_FILE)
    ap.add_argument("--out-root", default=DEFAULT_OUT_ROOT)
    ap.add_argument("--interval", type=float, default=POLL_SEC)
    ap.add_argument("--once", action="store_true", help="one poll then exit (smoke test)")
    args = ap.parse_args(argv)

    legs: dict[str, str] = {}
    last_refresh = 0.0
    written = 0

    while True:
        now = dt.datetime.now(IST)
        if not args.once and not in_session(now):
            if now.time() > SESSION_END or now.weekday() >= 5:
                print(f"session over ({written} rows written); exiting.")
                return 0
            time.sleep(args.interval)
            continue

        if time.time() - last_refresh > REFRESH_SEC or not legs:
            new = resolve_legs(args.positions_file)
            if new != legs:
                print(f"tracking {len(new)} leg(s): {', '.join(sorted(new)) or '(none open)'}")
            legs = new
            last_refresh = time.time()

        day = now.strftime("%Y%m%d")
        for sym, spec in legs.items():
            q = get(f"/tick/{spec}")
            if not q:
                continue
            append_row(
                out_path(args.out_root, sym, day),
                {
                    "poll_time": now.isoformat(),
                    "tick_time": q.get("ts", ""),
                    "symbol": sym,
                    "token": spec.split("|", 1)[1],
                    "ltp": q.get("lp", ""),
                    "bid": q.get("bp1", ""),
                    "ask": q.get("sp1", ""),
                    "bid_qty": q.get("bq1", ""),
                    "ask_qty": q.get("sq1", ""),
                    "oi": q.get("oi", ""),
                    "volume": q.get("v", ""),
                },
            )
            written += 1

        if args.once:
            print(f"{written} row(s) written.")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
