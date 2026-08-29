"""Tests for in-process tick persistence (tick_persist.py)."""

import csv
import datetime as dt
import os
from unittest.mock import MagicMock

import tick_persist


def _feed(subs, quotes):
    f = MagicMock()
    f.status.return_value = {"subscriptions": subs}
    f.get_quote.side_effect = lambda e, t, max_age_sec=None: quotes.get(f"{e}|{t}")
    return f


def _rows(root, symbol):
    day = dt.datetime.now(tick_persist.IST).strftime("%Y%m%d")
    p = os.path.join(root, f"market_data_{day}", "raw_data", "ticks", f"{symbol}_{day}.csv")
    with open(p) as f:
        return list(csv.DictReader(f))


def test_snapshot_writes_bid_ask_for_each_subscribed_instrument(tmp_path):
    """The whole point: leg-level bid/ask reaches disk. Without it,
    actionable_pnl cannot be replayed and exit rules stay unmeasurable."""
    tick_persist.register_symbols({"NFO|42669": "NIFTY08SEP26C24700"})
    feed = _feed(
        ["NFO|42669"],
        {"NFO|42669": {"lp": 40.7, "bp1": 40.5, "sp1": 40.9, "bq1": 75, "sq1": 50,
                       "oi": 477815, "v": 1101165, "ts": "1787651940"}},
    )
    assert tick_persist.snapshot_once(feed, str(tmp_path)) == 1
    r = _rows(str(tmp_path), "NIFTY08SEP26C24700")[0]
    assert r["bid"] == "40.5" and r["ask"] == "40.9" and r["ltp"] == "40.7"
    assert r["instrument"] == "NFO|42669"
    assert r["tick_time"] == "1787651940"
    assert r["snap_time"]


def test_unnamed_instrument_still_persists_under_its_spec(tmp_path):
    """A caller that does not supply a symbol map must cost readability only —
    never data."""
    feed = _feed(["MCX|999111"], {"MCX|999111": {"lp": 158187.0, "bp1": 158156.0, "sp1": 158203.0}})
    assert tick_persist.snapshot_once(feed, str(tmp_path)) == 1
    assert _rows(str(tmp_path), "MCX_999111")[0]["ltp"] == "158187.0"


def test_repeated_snapshots_append_rather_than_truncate(tmp_path):
    """Regular sampling is what makes the output a time series; a second pass
    must extend the file, and write the header exactly once."""
    tick_persist.register_symbols({"NFO|1": "SYMA"})
    feed = _feed(["NFO|1"], {"NFO|1": {"lp": 1.0, "bp1": 0.9, "sp1": 1.1}})
    tick_persist.snapshot_once(feed, str(tmp_path))
    tick_persist.snapshot_once(feed, str(tmp_path))
    rows = _rows(str(tmp_path), "SYMA")
    assert len(rows) == 2
    assert all(r["ltp"] == "1.0" for r in rows)


def test_snapshot_survives_a_failing_feed(tmp_path):
    """The writer shares a process with quote serving, so it must never raise:
    a status() or get_quote() fault has to degrade to zero rows."""
    bad = MagicMock()
    bad.status.side_effect = RuntimeError("feed down")
    assert tick_persist.snapshot_once(bad, str(tmp_path)) == 0

    partial = MagicMock()
    partial.status.return_value = {"subscriptions": ["NFO|1", "NFO|2"]}
    partial.get_quote.side_effect = [RuntimeError("boom"), {"lp": 5.0}]
    assert tick_persist.snapshot_once(partial, str(tmp_path)) == 1


def test_missing_or_empty_tick_is_skipped_not_written_blank(tmp_path):
    """An instrument subscribed but not yet ticking must produce no row —
    a blank row would look like a real observation of a zero price."""
    feed = _feed(["NFO|1", "NFO|2"], {"NFO|2": {"lp": 3.0}})
    assert tick_persist.snapshot_once(feed, str(tmp_path)) == 1


def test_start_is_opt_in(tmp_path):
    """Off unless a directory is configured: this writes to disk on a small box,
    so it should be a deliberate choice."""
    assert tick_persist.start(MagicMock(), None) is None
    assert tick_persist.start(MagicMock(), "") is None
