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
    output is still readable as a time series. The heartbeat is gated by the
    instrument's own exchange session (see in_session): the proxy now runs to
    23:58 for MCX, and NSE/NFO must not keep ticking a dead quote for the eight
    hours after they close. This also settles the weekend case above.
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
import re
import threading
import time

log = logging.getLogger("tick-persist")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DEFAULT_INTERVAL_SEC = 5.0
# Longest gap tolerated between rows for an instrument that is not moving.
HEARTBEAT_SEC = 60.0

# Per-exchange session end (IST). The heartbeat exists to bound gaps in a LIVE
# series; once an exchange has closed there is no series to bound, only a frozen
# quote repeating itself. Without this gate the ~200 NSE/NFO instruments keep
# emitting a row a minute for the eight hours the proxy now stays up for MCX —
# ~96k byte-identical rows a day.
#
# NSE/NFO cash and F&O close 15:30; 15:40 matches the proxy's own buffer. MCX
# runs to 23:30, or 23:55 on US daylight-saving days, so its window is set past
# both. CDS closes 17:00.
_SESSION_START = (9, 0)
_SESSION_END = {
    "NSE": (15, 40),
    "NFO": (15, 40),
    "BSE": (15, 40),
    "BFO": (15, 40),
    "CDS": (17, 10),
    "MCX": (23, 58),
}

# Holiday calendars. NSE and MCX do NOT share one, and the difference is not a
# detail: on most days NSE is shut, MCX closes only its morning session and
# trades the evening as normal. Treating the NSE list as universal would throw
# away real commodity ticks on eleven days a year — a worse failure than the
# frozen rows this gate exists to prevent, because the data is unrecoverable.
_CALENDAR_YEARS = frozenset({2026})

# Both sessions closed — nothing trades on either exchange.
_FULL_HOLIDAYS = frozenset({
    "2026-01-26",  # Republic Day
    "2026-04-03",  # Good Friday
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-12-25",  # Christmas
})

# NSE shut all day; MCX morning shut but its EVENING session trades as usual.
_MCX_EVENING_ONLY = frozenset({
    "2026-01-15",  # Municipal Corporation Election - Maharashtra
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali-Balipratipada
    "2026-11-24",  # Prakash Gurpurb Sri Guru Nanak Dev
})
_MCX_EVENING_START = (17, 0)

# Dates that trade despite falling on a weekend (Diwali muhurat). Without this
# the weekday check would discard the one session of the year that only ever
# happens on a Sunday. The whole day is treated as open rather than the real
# ~1h window: a few frozen rows cost far less than missing the session, and
# regimetrader's own MUHURAT_SESSIONS is still empty pending the NSE circular.
_SPECIAL_SESSIONS = frozenset({
    "2026-11-08",  # Diwali Laxmi Pujan — muhurat trading, a Sunday
})

_calendar_warned = False


def in_session(spec: str, now: dt.datetime) -> bool:
    """Is this instrument's exchange trading at `now` (IST)?

    Fails OPEN wherever it does not know: an unrecognised exchange, or a year
    the calendars do not cover. A new segment or a new year should degrade to
    some redundant rows, never to a silent hole in the series — rows can be
    filtered later, ticks that were never written cannot be recovered. The
    weekend rule is the one exception applied to every exchange.
    """
    global _calendar_warned
    day = now.date().isoformat()

    if now.year not in _CALENDAR_YEARS:
        if not _calendar_warned:
            log.warning(
                "tick-persist: no holiday calendar for %d — persisting every day. "
                "Update _FULL_HOLIDAYS / _MCX_EVENING_ONLY in tick_persist.py.",
                now.year,
            )
            _calendar_warned = True
        return True

    # A muhurat date is open all day by design (see _SPECIAL_SESSIONS): the real
    # window is roughly an hour in the evening, well outside every normal
    # _SESSION_END, so gating it on those hours would suppress exactly the
    # session this exists to keep.
    if day in _SPECIAL_SESSIONS:
        return True

    # No Indian exchange trades at the weekend, so this holds for an
    # unrecognised one too — it is knowledge about the country, not the segment.
    if now.weekday() >= 5:
        return False

    exchange = spec.partition("|")[0].upper()
    end = _SESSION_END.get(exchange)
    if end is None:
        return True

    if day in _FULL_HOLIDAYS:
        return False

    if day in _MCX_EVENING_ONLY:
        # NSE and the rest are shut all day; MCX picks its evening up at 17:00.
        if exchange != "MCX":
            return False
        return _MCX_EVENING_START <= (now.hour, now.minute) <= end

    return _SESSION_START <= (now.hour, now.minute) <= end


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


# Noren symbols are not all filename-safe: the index arrives as "Nifty 50",
# which produces a path with a space that breaks naive globbing downstream.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(symbol: str) -> str:
    return _UNSAFE.sub("_", symbol).strip("_") or "unknown"


def path_for(root: str, symbol: str, day: str) -> str:
    """Where a given instrument's rows for a given day live."""
    return os.path.join(
        root, f"market_data_{day}", "raw_data", "ticks", f"{safe_name(symbol)}_{day}.csv"
    )


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

    def should_write(
        self, spec: str, quote: dict, now_mono: float, session_open: bool = True
    ) -> bool:
        sig = tuple(quote.get(f) for f in _CHANGE_FIELDS)
        prev = self._last.get(spec)
        if prev is not None and prev[0] == sig:
            # Outside its session an instrument gets no heartbeat at all. A
            # genuine post-close move still falls through and is written, so a
            # settlement print or an after-hours correction is not lost.
            if not session_open or now_mono - prev[1] < HEARTBEAT_SEC:
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


def snapshot_once(feed, writer: TickWriter, now: dt.datetime | None = None) -> int:
    """One pass over every subscribed instrument. Returns rows written.

    Never raises: this shares a process with quote serving, so a feed or disk
    fault has to degrade to fewer rows rather than kill the thread.

    `now` is injectable so tests are not silently clock-dependent: whether a row
    is written now depends on the session window, so a suite pinned to no
    particular time would pass or fail by the hour it happened to run.
    """
    try:
        subs = feed.status().get("subscriptions") or []
    except Exception as e:
        log.warning("tick-persist: status() failed: %s", e)
        return 0

    now = now or dt.datetime.now(IST)
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
        if not q or not writer.should_write(spec, q, now_mono, in_session(spec, now)):
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
