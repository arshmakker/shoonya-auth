"""tick_persist.py — snapshot the WS tick cache to per-instrument CSVs.

Runs INSIDE broker_proxy, which already owns the tick store. The alternative —
a separate collector process polling /tick over HTTP — costs a whole Python
interpreter (~30-80MB on a 1GB droplet) and a round-trip per instrument per
poll, to read memory this process already holds.

Why this exists: nothing persisted the option legs we trade. The market_data
collector only captures strikes near spot and never the traded wings;
tick_store is in-memory and overwritten every tick; the ws_subscribe_* scripts
log only subscription bookkeeping. Without leg-level bid/ask, actionable_pnl
cannot be replayed, so exit-rule questions can only be argued rather than
measured.

Design notes:

  - Snapshot thread, not a hook in the request path. A slow disk stalls only
    this thread.
  - Instruments come from the feed's live subscription list, so whatever is
    subscribed is persisted — option legs, MCX, index — with no per-caller
    wiring, and a mid-session roll is picked up automatically.
  - Names come from the tick itself: Noren sends the trading symbol as 'ts',
    which tick_store already passes through. No registry, no /subscribe
    protocol extension, and it works without any caller cooperating.
  - Writes on CHANGE, with a heartbeat. The subscription set is ~200
    instruments (ws_subscribe_chain alone spans 33 strikes x 2 x 3 expiries),
    and most are far-OTM strikes that requote rarely. Writing all of them every
    pass produced ~920k rows/day, the large majority byte-identical repeats of
    a dead quote — and, since the proxy can outlive a session, ~1.2M frozen
    rows over a weekend. A row is written when the quote actually moved, or
    when HEARTBEAT_SEC has passed with no write, so gaps stay bounded and the
    output is still readable as a time series.
  - File handles are cached per (symbol, day). Reopening ~200 files every 5s
    cost ~7-9 syscalls per row; cached handles cost one write, with a flush at
    the end of each pass so a kill loses at most one pass.
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
# Longest gap tolerated between rows for an instrument that is not moving.
HEARTBEAT_SEC = 60.0

COLUMNS = (
    "snap_time",   # when this process read the cache
    "feed_time",   # Noren 'ft' — differs from snap_time when the tick is stale
    "instrument",  # EXCHANGE|TOKEN
    "symbol",      # Noren 'ts'; falls back to the spec when the feed omits it
    "ltp",
    "bid",
    "ask",
    "bid_qty",
    "ask_qty",
    "oi",
    "volume",
)

# Fields that decide whether a quote actually moved. Deliberately excludes oi
# and volume: both ratchet on every trade in the underlying, so including them
# would make every quote look changed and defeat the filter.
_CHANGE_FIELDS = ("lp", "bp1", "sp1", "bq1", "sq1")


def path_for(root: str, symbol: str, day: str) -> str:
    """Where a given instrument's rows for a given day live."""
    return os.path.join(root, f"market_data_{day}", "raw_data", "ticks", f"{symbol}_{day}.csv")


class TickWriter:
    """Owns the open CSV handles and the change/heartbeat state for one run."""

    def __init__(self, root: str):
        self.root = root
        self._files: dict[tuple[str, str], tuple[object, csv.DictWriter]] = {}
        self._last: dict[str, tuple[tuple, float]] = {}  # spec -> (signature, written_at)

    def _writer(self, symbol: str, day: str) -> csv.DictWriter:
        key = (symbol, day)
        hit = self._files.get(key)
        if hit is None:
            path = path_for(self.root, symbol, day)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = open(path, "a", newline="")
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if fh.tell() == 0:  # free header check — no extra stat
                w.writeheader()
            self._files[key] = hit = (fh, w)
        return hit[1]

    def _close_other_days(self, day: str) -> None:
        """Release handles from a previous day so a session spanning midnight
        does not keep appending to yesterday's inode."""
        for key in [k for k in self._files if k[1] != day]:
            fh, _ = self._files.pop(key)
            try:
                fh.close()
            except OSError:
                pass

    def should_write(self, spec: str, quote: dict, now_mono: float) -> bool:
        sig = tuple(quote.get(f) for f in _CHANGE_FIELDS)
        prev = self._last.get(spec)
        if prev is not None and prev[0] == sig and now_mono - prev[1] < HEARTBEAT_SEC:
            return False
        self._last[spec] = (sig, now_mono)
        return True

    def write(self, symbol: str, day: str, row: dict) -> None:
        self._writer(symbol, day).writerow(row)

    def flush(self, day: str) -> None:
        self._close_other_days(day)
        for fh, _ in self._files.values():
            try:
                fh.flush()
            except OSError as e:
                log.warning("tick-persist: flush failed: %s", e)


def snapshot_once(feed, writer: TickWriter) -> int:
    """One pass over every subscribed instrument. Returns rows written.

    Never raises: this shares a process with quote serving, so a feed or disk
    fault has to degrade to fewer rows rather than kill the thread.
    """
    try:
        subs = feed.status().get("subscriptions") or []
    except Exception as e:
        log.warning("tick-persist: status() failed: %s", e)
        return 0

    now = dt.datetime.now(IST)
    day = now.strftime("%Y%m%d")
    snap_time = now.isoformat()
    now_mono = time.monotonic()
    written = 0

    for spec in subs:
        exchange, _, token = spec.partition("|")
        if not exchange or not token:
            continue
        try:
            # max_age_sec=inf: take whatever the feed last saw and record its age
            # via feed_time, rather than silently dropping a stale entry.
            q = feed.get_quote(exchange, token, max_age_sec=float("inf"))
        except Exception:
            continue
        if not q or not writer.should_write(spec, q, now_mono):
            continue
        symbol = q.get("ts") or spec.replace("|", "_")
        try:
            writer.write(
                symbol,
                day,
                {
                    "snap_time": snap_time,
                    "feed_time": q.get("ft", ""),
                    "instrument": spec,
                    "symbol": symbol,
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

    writer.flush(day)
    return written


def run_persist_loop(feed, root: str, interval: float) -> None:
    log.info("tick-persist: writing subscribed ticks to %s every %.1fs", root, interval)
    writer = TickWriter(root)
    total = 0
    while True:
        started = time.monotonic()
        total += snapshot_once(feed, writer)
        # Sleep the REMAINDER, so a slow pass does not compound into ever-widening
        # gaps in the series.
        time.sleep(max(0.5, interval - (time.monotonic() - started)))


def start(feed, root: str | None, interval: float = DEFAULT_INTERVAL_SEC):
    """Start the writer thread. Returns the thread, or None when disabled."""
    root = (root or "").strip()
    if not root:
        return None
    t = threading.Thread(
        target=run_persist_loop,
        args=(feed, os.path.expanduser(root), interval),
        daemon=True,
        name="tick-persist",
    )
    t.start()
    return t
