#!/usr/bin/env python3
"""Audit a day of tick_persist output — coverage, frozen quotes, spread, depth.

Why: tick_persist writes a heartbeat row every HEARTBEAT_SEC even when the
quote has not moved, so a dead WS subscription produces a file that is fully
populated and completely wrong. On 2026-09-01 three MCX contracts stopped
updating at 14:43 IST and kept writing an unchanged 278.7 for six hours;
nothing noticed, because /feed/status still listed them as subscribed, its
last_msg_age_sec stayed at 0.03 on the strength of the 235 healthy tokens, and
the CSVs kept growing. Six hours of a liquid contract is worse than a missing
file, because a flat line reads as fact.

    # today, on the droplet
    python3 tools/tick_quality.py

    # a past day, one family, from a backup tree. --only matches the FILENAME,
    # which is the trading symbol — so "GOLD", not "MCX".
    python3 tools/tick_quality.py --day 20260831 --only GOLD \
        --root ~/git/trading/shoonya-auth/droplet_backup_20260901/regimetrader

Not every frozen run is a fault. A far-OTM option two weeks out can genuinely
hold one quote for 40 minutes, and on a normal day a handful do. Two signals
separate those from a dead subscription, and only they set the exit status:

  - DURATION. Nothing liquid holds a quote past --alarm-min (default 60).
  - CLUSTERED ONSET. Independent illiquidity does not synchronise. Three MCX
    contracts and a BANKNIFTY future all stopping inside the same 60 seconds is
    a feed event, whatever their individual durations say.

Comparing volume across the run does NOT work as a third signal, though it is
the obvious idea: a stalled subscription replays its whole last tick, so the
volume field freezes along with the quote and looks exactly like an instrument
that simply is not trading.

Exit status is 1 when either signal fires, so cron can pipe it at ntfy:

    30 * * * 1-5 python3 tools/tick_quality.py --quiet || \
        curl -d "MCX/NFO tick feed frozen — see tick_quality" ntfy.sh/<topic>

Reads .csv and .csv.gz alike (compress_old_ticks.sh gzips days older than a
week), and needs nothing but the stdlib so it runs under the droplet's system
python without the venv.

Definitions, since the naive versions are all wrong here:

  - A row is a HEARTBEAT if its change-signature (ltp, bid, ask, bid_qty,
    ask_qty) equals the previous row's. tick_persist only emits those on the
    HEARTBEAT_SEC timer, so a run of them is a quote that did not move.
  - FROZEN means an unbroken heartbeat run longer than --freeze-min. The run
    has to be measured on the signature, not on feed_time: a stalled feed keeps
    replaying its last tick's feed_time, so feed_time neither advances (which
    would look healthy) nor gaps (which would be easy to spot). It repeats.
  - The trailing heartbeat run at end-of-file is only reported when the day is
    still in progress. Every instrument is "frozen" after its exchange closes.
  - Runs ending before 09:17 IST are ignored: the NSE pre-open auction has no
    continuous quotes, so every NFO instrument looks frozen for exactly 15
    minutes, and the restored-cache rows at ~07:05 can extend that back two
    hours. Both are session, not fault.

Two artifacts are filtered, both of which otherwise poison any downstream
analysis:

  - the first rows of each file (~07:05 IST) are the restored tick cache
    stamped with today's date — yesterday's LTP, blank bid/ask. Dropped via the
    blank-bid check.
  - a handful of rows carry feed_time=0. Only --verbose counts them; nothing
    keys off feed_time, for the reason in the FROZEN note above.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import statistics
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / "git/trading/regimetrader"
# tick_persist.HEARTBEAT_SEC is 60; a run of unchanged rows this long is a
# quote that has not moved rather than a quiet instrument.
DEFAULT_FREEZE_MIN = 15.0
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Change-signature fields, mirroring tick_persist._CHANGE_FIELDS. oi and volume
# are deliberately excluded there and must be excluded here too, or every quote
# looks changed.
SIG_FIELDS = ("ltp", "bid", "ask", "bid_qty", "ask_qty")

# NSE/NFO pre-open auction: orders collect, nothing trades continuously. A run
# contained here is the session, not a fault. A little slack past 09:15
# absorbs the equilibrium-price print and the first continuous quote.
PREOPEN_START = dt.time(9, 0)
PREOPEN_END = dt.time(9, 17)


def is_preopen(start: dt.datetime, end: dt.datetime) -> bool:
    """Runs that end before any Indian exchange is trading.

    Two cases, and the second is the one that bites: a run inside the auction
    window, and a run that STARTS in the 07:05 restored-cache rows. On a day
    where that phantom quote happens to survive to 09:15, the second case is a
    ~130-minute frozen run on every single instrument — a guaranteed daily
    false alarm — so the test is on where the run ENDS, not where it starts.
    """
    return end.time() <= PREOPEN_END


def read_rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as fh:
        yield from csv.DictReader(fh)


def parse_snap(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class Series:
    """One instrument-day, reduced to the things that can be wrong with it."""

    def __init__(self, path: Path):
        self.path = path
        self.symbol = path.name.split("_")[0]
        self.rows = 0
        self.changes = 0          # rows where the quote actually moved
        self.bogus_feed_time = 0
        self.blank_quote = 0      # restored-cache phantoms and pre-open rows
        self.first: dt.datetime | None = None
        self.last: dt.datetime | None = None
        self.freezes: list[tuple[dt.datetime, dt.datetime]] = []
        self.trailing_freeze: tuple[dt.datetime, dt.datetime] | None = None
        self.spreads_bps: list[float] = []
        self.depths: list[int] = []

    def load(self, freeze_min: float) -> "Series":
        prev_sig = None
        run_start: dt.datetime | None = None
        run_end: dt.datetime | None = None
        threshold = freeze_min * 60

        for row in read_rows(self.path):
            when = parse_snap(row.get("snap_time", ""))
            if when is None:
                continue
            self.rows += 1
            if self.first is None:
                self.first = when
            self.last = when

            ft = row.get("feed_time") or ""
            # Anything below the 2023 epoch is not a timestamp. Counted only;
            # see the module docstring for why nothing keys off feed_time.
            if ft and ft.isdigit() and int(ft) < 1_700_000_000:
                self.bogus_feed_time += 1

            sig = tuple(row.get(f) for f in SIG_FIELDS)
            if prev_sig is not None and sig == prev_sig:
                if run_start is None:
                    run_start = when
                run_end = when
            else:
                self._close_run(run_start, run_end, threshold)
                run_start = run_end = None
                if prev_sig is not None:
                    self.changes += 1
            prev_sig = sig

            try:
                bid, ask = float(row["bid"]), float(row["ask"])
                bq, aq = int(row["bid_qty"]), int(row["ask_qty"])
            except (ValueError, TypeError, KeyError):
                self.blank_quote += 1
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                self.blank_quote += 1
                continue
            mid = (bid + ask) / 2
            self.spreads_bps.append((ask - bid) / mid * 1e4)
            self.depths.append(min(bq, aq))

        # A run still open at EOF is either a live freeze or the instrument's
        # own close. The caller decides, so keep it separate.
        if run_start and run_end and (run_end - run_start).total_seconds() > threshold:
            self.trailing_freeze = (run_start, run_end)
        return self

    def _close_run(self, start, end, threshold: float) -> None:
        if not start or not end:
            return
        if (end - start).total_seconds() <= threshold or is_preopen(start, end):
            return
        self.freezes.append((start, end))

    def frozen_minutes(self, include_trailing: bool) -> float:
        runs = list(self.freezes)
        if include_trailing and self.trailing_freeze:
            runs.append(self.trailing_freeze)
        return sum((b - a).total_seconds() for a, b in runs) / 60

    def longest_freeze(self, include_trailing: bool) -> float:
        """The alarm keys off the longest SINGLE run, not the daily total: six
        short quiet spells in an illiquid strike are not one dead hour."""
        runs = list(self.freezes)
        if include_trailing and self.trailing_freeze:
            runs.append(self.trailing_freeze)
        return max(((b - a).total_seconds() / 60 for a, b in runs), default=0.0)

    @property
    def med_spread_bps(self) -> float | None:
        return statistics.median(self.spreads_bps) if self.spreads_bps else None

    @property
    def med_depth(self) -> int | None:
        return round(statistics.median(self.depths)) if self.depths else None


def hhmm(t: dt.datetime | None) -> str:
    return t.strftime("%H:%M") if t else "-"


def onset_clusters(runs, window_sec: float = 90.0, min_size: int = 3):
    """Group freeze onsets that land within `window_sec` of each other.

    `runs` is (start, symbol). A cluster is the signature of a subscription
    being lost feed-wide; unrelated instruments going quiet is not
    synchronised to the minute.
    """
    clusters, current = [], []
    for start, symbol in sorted(runs):
        if current and (start - current[0][0]).total_seconds() <= window_sec:
            current.append((start, symbol))
            continue
        if len(current) >= min_size:
            clusters.append(current)
        current = [(start, symbol)]
    if len(current) >= min_size:
        clusters.append(current)
    return clusters


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                    help="tree holding market_data_YYYYMMDD/ (default: %(default)s)")
    ap.add_argument("--day", default=dt.datetime.now(IST).strftime("%Y%m%d"),
                    help="YYYYMMDD (default: today IST)")
    ap.add_argument("--only", default="",
                    help="substring filter on the filename, e.g. MCX prefixes: GOLD, NIFTY")
    ap.add_argument("--freeze-min", type=float, default=DEFAULT_FREEZE_MIN,
                    help="unchanged for longer than this many minutes = frozen "
                         "(default: %(default)s)")
    ap.add_argument("--alarm-min", type=float, default=60.0,
                    help="a single frozen run longer than this sets exit 1 "
                         "(default: %(default)s)")
    ap.add_argument("--top", type=int, default=15,
                    help="how many frozen instruments to list (default: %(default)s)")
    ap.add_argument("--quiet", action="store_true",
                    help="print only frozen instruments — for cron")
    ap.add_argument("--verbose", action="store_true",
                    help="one line per instrument, including healthy ones")
    args = ap.parse_args()

    ticks = args.root.expanduser() / f"market_data_{args.day}" / "raw_data" / "ticks"
    if not ticks.is_dir():
        print(f"no tick directory: {ticks}", file=sys.stderr)
        return 2

    files = sorted(p for p in ticks.iterdir()
                   if p.suffix in (".csv", ".gz") and args.only in p.name)
    if not files:
        print(f"no tick files matching {args.only!r} in {ticks}", file=sys.stderr)
        return 2

    # Only a day still in progress can have a live freeze at end-of-file;
    # on a finished day the trailing run is just the close.
    in_progress = args.day == dt.datetime.now(IST).strftime("%Y%m%d")

    series = [Series(p).load(args.freeze_min) for p in files]
    frozen = [s for s in series
              if s.freezes or (in_progress and s.trailing_freeze)]

    if not args.quiet:
        print(f"{args.day}: {len(series)} instruments, "
              f"{sum(s.rows for s in series):,} rows"
              + ("  (day in progress)" if in_progress else ""))
        print()

    if args.verbose and not args.quiet:
        print(f"{'symbol':24} {'rows':>7} {'moves':>7} {'first':>6} {'last':>6} "
              f"{'spr_bps':>8} {'depth':>6} {'frozen_m':>9}")
        for s in sorted(series, key=lambda x: -x.frozen_minutes(in_progress)):
            spr = f"{s.med_spread_bps:.1f}" if s.med_spread_bps is not None else "-"
            print(f"{s.symbol:24} {s.rows:7,} {s.changes:7,} {hhmm(s.first):>6} "
                  f"{hhmm(s.last):>6} {spr:>8} {str(s.med_depth):>6} "
                  f"{s.frozen_minutes(in_progress):9.0f}")
        print()

    if frozen:
        print(f"FROZEN — {len(frozen)} instrument(s) stopped updating "
              f"while the file kept growing:")
        ranked = sorted(frozen, key=lambda x: -x.frozen_minutes(in_progress))
        for s in ranked[:args.top]:
            runs = list(s.freezes)
            if in_progress and s.trailing_freeze:
                runs.append(s.trailing_freeze)
            spans = ", ".join(f"{hhmm(a)}-{hhmm(b)}" for a, b in runs[:4])
            more = f" (+{len(runs) - 4} more)" if len(runs) > 4 else ""
            print(f"  {s.symbol:24} {s.frozen_minutes(in_progress):5.0f} min  {spans}{more}")
        if len(ranked) > args.top:
            print(f"  ... and {len(ranked) - args.top} more (--top N to see them)")
        print()

    all_runs = [(a, s.symbol) for s in frozen for a, _ in s.freezes]
    if in_progress:
        all_runs += [(s.trailing_freeze[0], s.symbol)
                     for s in frozen if s.trailing_freeze]
    clusters = onset_clusters(all_runs)
    longest = max((s.longest_freeze(in_progress) for s in series), default=0.0)

    for c in clusters:
        print(f"CLUSTERED ONSET at {hhmm(c[0][0])} — {len(c)} instruments stopped "
              f"within 90s of each other:")
        print("  " + ", ".join(sym for _, sym in c))
        print("  Synchronised across unrelated instruments: a lost subscription, "
              "not illiquidity.")
        print()

    if not clusters and longest <= args.alarm_min and frozen and not args.quiet:
        print(f"None of these trips the alarm: longest run {longest:.0f} min "
              f"(< {args.alarm_min:.0f}) and no clustered onset — consistent with "
              f"illiquid instruments holding a quote.")

    if clusters or longest > args.alarm_min:
        print("  No known recovery, and the obvious three all failed on 2026-09-01:")
        print("    - /subscribe returns ONE snapshot, then streaming stops again")
        print("    - trimming the subscription count 239 -> 175 changed nothing")
        print("    - a full broker_proxy restart (new WS session) changed nothing:")
        print("      25 of 28 MCX contracts streamed, the same 3 stayed dead")
        print("  The fault is bound to the token on the broker side and survives a")
        print("  brand-new connection. Untested: a fresh OAuth login (start_vps.sh")
        print("  blanks Access_token), which is the only lever left that changes")
        print("  the broker-side session rather than the socket.")
        return 1

    if not args.quiet:
        if not frozen:
            print("No frozen instruments.")
        odd = [s for s in series if s.bogus_feed_time or s.blank_quote > 10]
        if odd and args.verbose:
            print("\nArtifacts (expected, filter before analysis):")
            for s in odd:
                print(f"  {s.symbol:24} feed_time=0 x{s.bogus_feed_time}, "
                      f"blank/pre-open quotes x{s.blank_quote}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
