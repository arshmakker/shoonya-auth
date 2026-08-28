"""tools/mcx_ws_subscribe.py — subscribe the MCX liquid-5 commodity futures
(and their mini/micro variants) on broker_proxy's WS feed (touchline only,
no order placement).

Scope matches regimetrader/tools/mcx_collector.py's ALL_UNDERLYINGS: GOLD,
SILVER, CRUDEOIL, COPPER, NATURALGAS (liquid-5) plus GOLDM, GOLDTEN,
GOLDGUINEA, GOLDPETAL, SILVERM, SILVERMIC, SILVER100, CRUDEOILM, NATGASMINI
(minis — no mini exists for COPPER) — front-2 non-expired FUTCOM contracts
each, 28 contracts total. Unlike mcx_collector.py (REST poll -> CSV), this
puts the same instruments on the shared WS feed so their touchline is cached
alongside the NSE/NFO subscriptions and readable via GET /tick/<key>.

Tokens come straight from regimetrader/symbols/MCX.csv, so no searchscrip
round-trip is needed (unlike ws_subscribe_chain.py's NFO symbols, which are
built as trading-symbol strings and must be resolved to a token).

Run AFTER broker_proxy is healthy:
    ./venv/bin/python tools/mcx_ws_subscribe.py
"""

import csv
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from proxy_client import post

LIQUID_5 = ("GOLD", "SILVER", "CRUDEOIL", "COPPER", "NATURALGAS")
MINIS = (
    "GOLDM", "GOLDTEN", "GOLDGUINEA", "GOLDPETAL",
    "SILVERM", "SILVERMIC", "SILVER100",
    "CRUDEOILM", "NATGASMINI",
)  # no mini exists for COPPER
ALL_UNDERLYINGS = LIQUID_5 + MINIS
DEFAULT_MASTER = os.path.join(
    os.path.dirname(__file__), "..", "..", "regimetrader", "symbols", "MCX.csv"
)


def parse_mcx_master(path):
    """Parse MCX.csv. Returns one dict per row. Mirrors mcx_collector.py's
    parser — MCX schema differs from NFO (extra GNGD column at index 3)."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header[:8] != [
            "Exchange",
            "Token",
            "LotSize",
            "GNGD",
            "Symbol",
            "TradingSymbol",
            "Expiry",
            "Instrument",
        ]:
            raise ValueError(f"MCX.csv schema changed; got header: {header}")
        for r in reader:
            if len(r) < 8 or not r[0]:
                continue
            rows.append(
                {
                    "exchange": r[0],
                    "token": r[1],
                    "symbol": r[4],
                    "trading_symbol": r[5],
                    "expiry": r[6],
                    "instrument": r[7],
                }
            )
    return rows


def select_front_two(rows, underlyings, today):
    """For each underlying, return the two earliest non-expired FUTCOM contracts."""
    targets = set(underlyings)
    by_underlying = {u: [] for u in targets}
    for r in rows:
        if r["instrument"] != "FUTCOM" or r["symbol"] not in targets:
            continue
        try:
            exp = datetime.strptime(r["expiry"], "%d-%b-%Y").date()
        except ValueError:
            continue
        if exp < today:
            continue
        by_underlying[r["symbol"]].append({**r, "expiry_date": exp})
    selected = []
    for u in underlyings:
        contracts = sorted(by_underlying.get(u, []), key=lambda x: x["expiry_date"])
        selected.extend(contracts[:2])
    return selected


def main():
    master = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MASTER)
    rows = parse_mcx_master(master)
    contracts = select_front_two(rows, ALL_UNDERLYINGS, date.today())

    missing = [u for u in ALL_UNDERLYINGS if not any(c["symbol"] == u for c in contracts)]
    specs = [f"{c['exchange']}|{c['token']}" for c in contracts]

    print(f"selected {len(contracts)}/{len(ALL_UNDERLYINGS) * 2} contracts: "
          + ", ".join(c["trading_symbol"] for c in contracts))
    if missing:
        print(f"no non-expired FUTCOM found for: {', '.join(missing)}")

    result = post("/subscribe", {"instruments": specs})
    print("SUBSCRIBE:", result)


if __name__ == "__main__":
    main()
