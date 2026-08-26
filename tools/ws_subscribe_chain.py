#!/usr/bin/env python
"""ws_subscribe_chain.py — boot-time wide-net subscription for hybrid mode.

Subscribes the NIFTY index plus CE+PE touchlines for every weekly strike in
a window around live spot, so cache-first quote serving has teeth from the
first strategy call of the day. Resolves symbols via broker_proxy
searchscrip; unresolved symbols are skipped with a warning.

Run AFTER broker_proxy is healthy (start_vps.sh backgrounds this):
    ./venv/bin/python tools/ws_subscribe_chain.py [spot] [--width N]
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from proxy_client import post, resolve

_RESOLVE_WORKERS = 16

INDEX_SPEC = "NSE|26000"
_POSITION_LEG_KEYS = ("sc_sym", "sp_sym", "lc_sym", "lp_sym")
DEFAULT_POSITIONS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "regimetrader", "data", "open_positions.json"
)


def next_weekly_expiry(today):
    """Nearest TUESDAY (NSE NIFTY weekly convention), inclusive of today."""
    offset = (1 - today.weekday()) % 7
    return today + timedelta(days=offset)


def weekly_expiries_to_subscribe(today):
    """Three rolling Tuesday expiries (current + next two): the strategy can
    skip a weak-credit week entirely (2026-08-25: Sep 1 rolled to Sep 8), so
    two weeks of coverage misses the contract actually traded."""
    current = next_weekly_expiry(today)
    return current, current + timedelta(days=7), current + timedelta(days=14)


def nifty_chain_symbols(expiry, spot, width=800, step=50):
    lo = -(-int(spot - width) // step) * step
    hi = int(spot + width) // step * step
    tag = expiry.strftime("%d%b%y").upper()
    out = []
    for strike in range(lo, hi + step, step):
        out.append(f"NIFTY{tag}C{strike}")
        out.append(f"NIFTY{tag}P{strike}")
    return out


def position_leg_symbols(positions_path):
    """Bare NFO trading symbols for any open position's option legs.

    A position entered on an earlier day can hold wing strikes that later
    drift outside the spot-window chain (2026-08-26: two long legs sat
    outside +-800pt of the day's spot and lost WS coverage for the whole
    session). Persisted leg fields are 'NFO|<symbol>', not a resolved
    token spec, so callers still need resolve() on the returned symbols.
    Scans recursively since the leg fields' nesting depth is whatever the
    owning strategy's save_state() produces, not a fixed schema.
    """
    try:
        with open(positions_path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []

    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _POSITION_LEG_KEYS and isinstance(v, str) and v:
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return [sym.split("|", 1)[1] if "|" in sym else sym for sym in found]


def fetch_spot():
    quote = post("/call", {"method": "get_quotes", "args": ["NSE", "26000"]}) or {}
    return float(quote.get("lp") or 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--positions-file", default=DEFAULT_POSITIONS_FILE)
    parser.add_argument("spot", nargs="?", type=float, default=None)
    args = parser.parse_args()

    spot = args.spot or fetch_spot()
    if not spot:
        print("ERROR: no spot available; pass it as an argument")
        sys.exit(1)

    expiries = weekly_expiries_to_subscribe(date.today())
    symbols = []
    seen = set()
    for expiry in expiries:
        for sym in nifty_chain_symbols(expiry, spot, width=args.width):
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)

    # An open position's own legs must always be covered, even when they've
    # drifted outside the spot window (2026-08-26 root cause of the day's
    # stale-LTP warnings: both long wings sat outside +-800pt of spot).
    leg_symbols = [s for s in position_leg_symbols(args.positions_file) if s not in seen]
    for sym in leg_symbols:
        seen.add(sym)
        symbols.append(sym)
    if leg_symbols:
        print(f"open position legs outside/inside window: {', '.join(leg_symbols)}")

    print(f"spot={spot} expiries={','.join(e.strftime('%d%b%y') for e in expiries)} — resolving {len(symbols)} chain symbols")

    specs = [INDEX_SPEC]
    missing = []
    # Resolutions are independent /call round-trips to the local proxy — fan
    # them out instead of paying (network latency x len(symbols)) serially
    # at boot, while this backgrounds ahead of the first strategy call.
    with ThreadPoolExecutor(max_workers=_RESOLVE_WORKERS) as pool:
        for sym, spec in zip(symbols, pool.map(resolve, symbols)):
            if spec:
                specs.append(spec)
            else:
                missing.append(sym)

    print(f"resolved {len(specs) - 1}/{len(symbols)}; subscribing {len(specs)} total")
    result = post("/subscribe", {"instruments": specs})
    print("SUBSCRIBE:", result)
    if missing:
        print(f"unresolved ({len(missing)}): {', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")


if __name__ == "__main__":
    main()
