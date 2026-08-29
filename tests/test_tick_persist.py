"""Tests for in-process tick persistence (tick_persist.py)."""

import csv
import datetime as dt
from unittest.mock import MagicMock

import tick_persist
from tick_persist import TickWriter


def _feed(subs, quotes):
    f = MagicMock()
    f.status.return_value = {"subscriptions": subs}
    f.get_quote.side_effect = lambda e, t, max_age_sec=None: quotes.get(f"{e}|{t}")
    return f


def _rows(root, symbol):
    """Read back via path_for(), so a layout change fails these tests rather
    than silently breaking every downstream consumer."""
    day = dt.datetime.now(tick_persist.IST).strftime("%Y%m%d")
    with open(tick_persist.path_for(str(root), symbol, day)) as f:
        return list(csv.DictReader(f))


def test_snapshot_writes_bid_ask_and_names_the_file_from_the_tick(tmp_path):
    """The whole point: leg-level bid/ask reaches disk. The symbol comes from
    Noren's 'ts' passthrough, so no caller has to register a name."""
    feed = _feed(
        ["NFO|42669"],
        {"NFO|42669": {"ts": "NIFTY08SEP26C24700", "ft": "1787651940", "lp": 40.7,
                       "bp1": 40.5, "sp1": 40.9, "bq1": 75, "sq1": 50,
                       "oi": 477815, "v": 1101165}},
    )
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path))) == 1
    r = _rows(tmp_path, "NIFTY08SEP26C24700")[0]
    assert r["bid"] == "40.5" and r["ask"] == "40.9" and r["ltp"] == "40.7"
    assert r["instrument"] == "NFO|42669"
    # 'ft' is the feed clock and 'ts' the symbol — not interchangeable.
    assert r["feed_time"] == "1787651940"
    assert r["symbol"] == "NIFTY08SEP26C24700"
    assert r["snap_time"]


def test_unnamed_instrument_still_persists_under_its_spec(tmp_path):
    """A feed that omits 'ts' must cost readability only — never data."""
    feed = _feed(["MCX|999111"], {"MCX|999111": {"lp": 158187.0, "bp1": 158156.0, "sp1": 158203.0}})
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path))) == 1
    assert _rows(tmp_path, "MCX_999111")[0]["ltp"] == "158187.0"


def test_unchanged_quote_is_not_rewritten_every_pass(tmp_path):
    """~200 instruments are subscribed and most are far-OTM strikes that requote
    rarely. Rewriting a frozen quote every pass produced ~920k rows/day, mostly
    byte-identical repeats — and kept doing so all weekend."""
    feed = _feed(["NFO|1"], {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1}})
    w = TickWriter(str(tmp_path))
    assert tick_persist.snapshot_once(feed, w) == 1
    assert tick_persist.snapshot_once(feed, w) == 0
    assert tick_persist.snapshot_once(feed, w) == 0
    assert len(_rows(tmp_path, "SYMA")) == 1


def test_a_moved_quote_is_written_again(tmp_path):
    """The suppression must be on the quote, not on time: a real move records
    immediately, and rows accumulate under one header."""
    quotes = {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1}}
    feed = _feed(["NFO|1"], quotes)
    w = TickWriter(str(tmp_path))
    tick_persist.snapshot_once(feed, w)
    quotes["NFO|1"] = {"ts": "SYMA", "lp": 1.2, "bp1": 1.1, "sp1": 1.3}
    assert tick_persist.snapshot_once(feed, w) == 1
    rows = _rows(tmp_path, "SYMA")
    assert [r["ltp"] for r in rows] == ["1.0", "1.2"]


def test_heartbeat_bounds_the_gap_for_a_still_instrument(tmp_path):
    """Change-detection must not let an instrument vanish from the series
    entirely — after HEARTBEAT_SEC an unchanged quote is recorded anyway."""
    feed = _feed(["NFO|1"], {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1}})
    w = TickWriter(str(tmp_path))
    tick_persist.snapshot_once(feed, w)
    w._last["NFO|1"] = (w._last["NFO|1"][0], w._last["NFO|1"][1] - tick_persist.HEARTBEAT_SEC - 1)
    assert tick_persist.snapshot_once(feed, w) == 1


def test_oi_and_volume_do_not_count_as_a_quote_change(tmp_path):
    """Both ratchet on every trade in the underlying. Counting them would make
    every quote look changed and defeat the filter entirely."""
    quotes = {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1, "oi": 10, "v": 100}}
    feed = _feed(["NFO|1"], quotes)
    w = TickWriter(str(tmp_path))
    tick_persist.snapshot_once(feed, w)
    quotes["NFO|1"] = {**quotes["NFO|1"], "oi": 11, "v": 250}
    assert tick_persist.snapshot_once(feed, w) == 0


def test_snapshot_survives_a_failing_feed(tmp_path):
    """The writer shares a process with quote serving, so it must never raise:
    a status() or get_quote() fault has to degrade to fewer rows."""
    bad = MagicMock()
    bad.status.side_effect = RuntimeError("feed down")
    assert tick_persist.snapshot_once(bad, TickWriter(str(tmp_path))) == 0

    partial = MagicMock()
    partial.status.return_value = {"subscriptions": ["NFO|1", "NFO|2"]}
    partial.get_quote.side_effect = [RuntimeError("boom"), {"ts": "SYMB", "lp": 5.0}]
    assert tick_persist.snapshot_once(partial, TickWriter(str(tmp_path))) == 1


def test_missing_or_empty_tick_is_skipped_not_written_blank(tmp_path):
    """An instrument subscribed but not yet ticking must produce no row — a
    blank row would read as a real observation of a zero price."""
    feed = _feed(["NFO|1", "NFO|2"], {"NFO|2": {"ts": "SYMB", "lp": 3.0}})
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path))) == 1


def test_day_rollover_releases_the_previous_day_handle(tmp_path):
    """A session spanning midnight must not keep appending to yesterday's
    inode."""
    w = TickWriter(str(tmp_path))
    w.write("SYMA", "20260828", dict.fromkeys(tick_persist.COLUMNS, ""))
    assert len(w._files) == 1
    w.flush("20260829")
    assert w._files == {}


def test_start_is_opt_in(tmp_path):
    """Off unless a directory is configured: this writes to disk on a small box,
    so it should be a deliberate choice."""
    assert tick_persist.start(MagicMock(), None) is None
    assert tick_persist.start(MagicMock(), "   ") is None
