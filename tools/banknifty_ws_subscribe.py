#!/usr/bin/env python
"""tools/banknifty_ws_subscribe.py — subscribe BankNifty's spot, near-month
future, and options on broker_proxy's WS feed (touchline only, no order
placement).

Unlike ws_subscribe_chain.py (NIFTY), this does NOT rebuild a strike window
from scratch — BankNifty's expiry cadence isn't the same weekly-Tuesday
convention NIFTY uses (regimetrader's own SymbolManager already resolved
that once today). Instead it reads today's already-written
market_data_YYYYMMDD/raw_data/{futures,options/BANKNIFTY} filenames — the
exact instrument set regimetrader's DataCollector is already polling and
persisting via REST — and puts those same instruments on the WS feed too.
This is what protects them from the spot-contamination REST quirk the way
the NIFTY chain and open-IC legs already are, without guessing at
BankNifty's strike/expiry rules independently.

Run AFTER broker_proxy is healthy and DataCollector has done its first
symbol-selection pass for the day (regimetrader writes those files within
the first minute of main.py starting):
    ./venv/bin/python tools/banknifty_ws_subscribe.py
"""

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from proxy_client import post

_RESOLVE_WORKERS = 8
BANKNIFTY_SPOT_SYMBOL = "Nifty Bank"
REGIME_MARKET_DATA_ROOT = Path(os.path.dirname(__file__)) / ".." / ".." / "regimetrader"

_TRADING_SYMBOL_RE = re.compile(r"^(.+)_\d{8}\.csv$")


def _trading_symbol_from_filename(fname):
    m = _TRADING_SYMBOL_RE.match(fname)
    return m.group(1) if m else None


def discover_banknifty_symbols(market_data_dir):
    """Trading symbols regimetrader is already tracking today for BankNifty:
    the near-month future plus every CE/PE file under raw_data/options/BANKNIFTY."""
    symbols = []

    futures_dir = market_data_dir / "raw_data" / "futures"
    if futures_dir.is_dir():
        for f in sorted(futures_dir.iterdir()):
            sym = _trading_symbol_from_filename(f.name)
            if sym and sym.startswith("BANKNIFTY"):
                symbols.append(sym)

    options_dir = market_data_dir / "raw_data" / "options" / "BANKNIFTY"
    for side in ("ce", "pe"):
        side_dir = options_dir / side
        if not side_dir.is_dir():
            continue
        for f in sorted(side_dir.iterdir()):
            sym = _trading_symbol_from_filename(f.name)
            if sym:
                symbols.append(sym)

    return symbols


def resolve(symbol, exchange="NFO"):
    res = post("/call", {"method": "searchscrip", "args": [exchange, symbol]}) or {}
    vals = res.get("values") or []
    return f"{exchange}|{vals[0]['token']}" if vals else None


def main():
    today = datetime.now().strftime("%Y%m%d")
    market_data_dir = REGIME_MARKET_DATA_ROOT / f"market_data_{today}"

    symbols = discover_banknifty_symbols(market_data_dir)
    if not symbols:
        print(f"no BankNifty files found under {market_data_dir} — has DataCollector run yet today?")
        sys.exit(1)

    print(f"found {len(symbols)} BankNifty instruments already tracked today: {', '.join(symbols)}")

    specs = []
    missing = []
    spot_spec = resolve(BANKNIFTY_SPOT_SYMBOL, exchange="NSE")
    if spot_spec:
        specs.append(spot_spec)
    else:
        missing.append(BANKNIFTY_SPOT_SYMBOL)

    with ThreadPoolExecutor(max_workers=_RESOLVE_WORKERS) as pool:
        for sym, spec in zip(symbols, pool.map(resolve, symbols)):
            if spec:
                specs.append(spec)
            else:
                missing.append(sym)

    print(f"resolved {len(specs)}/{len(symbols) + 1}; subscribing {len(specs)} total")
    result = post("/subscribe", {"instruments": specs})
    print("SUBSCRIBE:", result)
    if missing:
        print(f"unresolved ({len(missing)}): {', '.join(missing)}")


if __name__ == "__main__":
    main()
