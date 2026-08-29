"""tick_persist.py — snapshot the WS tick cache to per-instrument CSVs.

Runs INSIDE broker_proxy, which already owns the tick store. The alternative —
a separate collector process polling /tick over HTTP — costs a whole Python
interpreter (~30-80MB on a 1GB droplet) and a round-trip per instrument per
poll, to read memory this process already holds.

Why this exists at all: nothing persisted the option legs we trade. The
market_data collector only captures strikes near spot and never the traded
wings; tick_store is in-memory and overwritten every tick; the ws_subscribe_*
scripts log only subscription bookkeeping. Without leg-level bid/ask,
actionable_pnl cannot be replayed, so exit-rule questions can only be argued
rather than measured.

Design notes:

  - This is a SNAPSHOT thread, not a hook in the request path. It reads the
    store on a timer and writes; a slow disk stalls only this thread. File I/O
    releases the GIL, so quote serving is unaffected.
  - Every tick the feed holds is written on each pass, including unchanged ones:
    regular sampling is what makes the output usable as a time series. The
    feed's own timestamp is recorded next to the local snapshot time, so a stale
    cache entry is visible rather than silently looking like a fresh quote.
  - Instruments are discovered from the feed's live subscription list, so
    whatever is subscribed gets persisted — option legs, MCX, index — with no
    per-caller wiring. A roll mid-session is picked up automatically.
  - Off unless SHOONYA_TICK_PERSIST_DIR is set. It writes to disk on a small
    box, so it should be a deliberate choice, not a default.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import os
import threading
import time

log = logging.getLogger("tick-persist")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DEFAULT_INTERVAL_SEC = 5.0

COLUMNS = (
    "snap_time",   # when this process read the cache
    "tick_time",   # the feed's own ts — differs from snap_time when the tick is stale
    "instrument",  # EXCHANGE|TOKEN
    "symbol",      # resolved name when known, else the instrument spec
    "ltp",
    "bid",
    "ask",
    "bid_qty",
    "ask_qty",
    "oi",
    "volume",
)

# instrument spec -> human symbol, populated opportunistically by /subscribe
# callers. Filenames fall back to the spec when a caller did not supply one, so
# a missing name costs readability, never data.
_symbols: dict[str, str] = {}
_symbols_lock = threading.Lock()


def register_symbols(mapping: dict) -> None:
    if not isinstance(mapping, dict):
        return
    with _symbols_lock:
        for spec, name in mapping.items():
            if isinstance(spec, str) and isinstance(name, str) and spec and name:
                _symbols[spec] = name


def symbol_for(spec: str) -> str:
    with _symbols_lock:
        return _symbols.get(spec) or spec.replace("|", "_")


def _path(root: str, symbol: str, day: str) -> str:
    d = os.path.join(root, f"market_data_{day}", "raw_data", "ticks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}_{day}.csv")


def _append(path: str, row: dict) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


def snapshot_once(feed, root: str) -> int:
    """One pass over every subscribed instrument. Returns rows written."""
    try:
        subs = feed.status().get("subscriptions") or []
    except Exception as e:  # a status hiccup must never kill the thread
        log.warning("tick-persist: status() failed: %s", e)
        return 0

    now = dt.datetime.now(IST)
    day = now.strftime("%Y%m%d")
    written = 0
    for spec in subs:
        exchange, _, token = spec.partition("|")
        if not exchange or not token:
            continue
        try:
            # max_age_sec=inf: we want whatever the feed last saw, and record its
            # age via tick_time rather than silently dropping a stale entry.
            q = feed.get_quote(exchange, token, max_age_sec=float("inf"))
        except Exception:
            continue
        if not q:
            continue
        try:
            _append(
                _path(root, symbol_for(spec), day),
                {
                    "snap_time": now.isoformat(),
                    "tick_time": q.get("ts", ""),
                    "instrument": spec,
                    "symbol": symbol_for(spec),
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
        except OSError as e:
            log.warning("tick-persist: write failed for %s: %s", spec, e)
    return written


def run_persist_loop(feed, root: str, interval: float = DEFAULT_INTERVAL_SEC) -> None:
    log.info("tick-persist: writing subscribed ticks to %s every %.1fs", root, interval)
    total = 0
    while True:
        started = time.monotonic()
        try:
            total += snapshot_once(feed, root)
        except Exception as e:  # never let the writer take down the proxy
            log.warning("tick-persist: pass failed: %s", e)
        # Sleep the REMAINDER of the interval so a slow pass does not compound
        # into ever-widening gaps in the series.
        time.sleep(max(0.5, interval - (time.monotonic() - started)))
        if total and total % 5000 < 1:
            log.info("tick-persist: %d rows written this session", total)


def start(feed, root: str | None, interval: float = DEFAULT_INTERVAL_SEC):
    """Start the writer thread. Returns the thread, or None when disabled."""
    if not root:
        return None
    t = threading.Thread(
        target=run_persist_loop, args=(feed, root, interval), daemon=True, name="tick-persist"
    )
    t.start()
    return t
