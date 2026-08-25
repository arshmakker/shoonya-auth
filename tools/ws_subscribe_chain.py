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
import sys
import urllib.request
from datetime import date, timedelta

PROXY = "http://127.0.0.1:7890"
INDEX_SPEC = "NSE|26000"


def post(path, payload, timeout=15):
    req = urllib.request.Request(
        PROXY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


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


def resolve(symbol):
    res = post("/call", {"method": "searchscrip", "args": ["NFO", symbol]}) or {}
    vals = res.get("values") or []
    return f"NFO|{vals[0]['token']}" if vals else None


def fetch_spot():
    quote = post("/call", {"method": "get_quotes", "args": ["NSE", "26000"]}) or {}
    return float(quote.get("lp") or 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=800)
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
    print(f"spot={spot} expiries={','.join(e.strftime('%d%b%y') for e in expiries)} — resolving {len(symbols)} chain symbols")

    specs = [INDEX_SPEC]
    missing = []
    for sym in symbols:
        spec = resolve(sym)
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
