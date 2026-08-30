"""Tests for in-process tick persistence (tick_persist.py)."""

import csv
import datetime as dt
from unittest.mock import MagicMock

import tick_persist
from tick_persist import TickWriter

# Fri 2026-08-28, 11:00 IST — inside every exchange session. Pinned because
# a row is only written when the instrument's exchange is open, so an
# unpinned suite would pass or fail depending on the hour it ran.
_MIDSESSION = dt.datetime(2026, 8, 28, 11, 0, tzinfo=tick_persist.IST)


def _feed(subs, quotes):
    f = MagicMock()
    f.status.return_value = {"subscriptions": subs}
    f.get_quote.side_effect = lambda e, t, max_age_sec=None: quotes.get(f"{e}|{t}")
    return f


def _rows(root, symbol):
    """Read back via path_for(), so a layout change fails these tests rather
    than silently breaking every downstream consumer."""
    day = _MIDSESSION.strftime("%Y%m%d")
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
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path)), _MIDSESSION) == 1
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
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path)), _MIDSESSION) == 1
    assert _rows(tmp_path, "MCX_999111")[0]["ltp"] == "158187.0"


def test_unchanged_quote_is_not_rewritten_every_pass(tmp_path):
    """~200 instruments are subscribed and most are far-OTM strikes that requote
    rarely. Rewriting a frozen quote every pass produced ~920k rows/day, mostly
    byte-identical repeats — and kept doing so all weekend."""
    feed = _feed(["NFO|1"], {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1}})
    w = TickWriter(str(tmp_path))
    assert tick_persist.snapshot_once(feed, w, _MIDSESSION) == 1
    assert tick_persist.snapshot_once(feed, w, _MIDSESSION) == 0
    assert tick_persist.snapshot_once(feed, w, _MIDSESSION) == 0
    assert len(_rows(tmp_path, "SYMA")) == 1


def test_a_moved_quote_is_written_again(tmp_path):
    """The suppression must be on the quote, not on time: a real move records
    immediately, and rows accumulate under one header."""
    quotes = {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1}}
    feed = _feed(["NFO|1"], quotes)
    w = TickWriter(str(tmp_path))
    tick_persist.snapshot_once(feed, w, _MIDSESSION)
    quotes["NFO|1"] = {"ts": "SYMA", "lp": 1.2, "bp1": 1.1, "sp1": 1.3}
    assert tick_persist.snapshot_once(feed, w, _MIDSESSION) == 1
    rows = _rows(tmp_path, "SYMA")
    assert [r["ltp"] for r in rows] == ["1.0", "1.2"]


def test_heartbeat_bounds_the_gap_for_a_still_instrument(tmp_path):
    """Change-detection must not let an instrument vanish from the series
    entirely — after HEARTBEAT_SEC an unchanged quote is recorded anyway."""
    feed = _feed(["NFO|1"], {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1}})
    w = TickWriter(str(tmp_path))
    tick_persist.snapshot_once(feed, w, _MIDSESSION)
    w._last["NFO|1"] = (w._last["NFO|1"][0], w._last["NFO|1"][1] - tick_persist.HEARTBEAT_SEC - 1)
    assert tick_persist.snapshot_once(feed, w, _MIDSESSION) == 1


def test_oi_and_volume_do_not_count_as_a_quote_change(tmp_path):
    """Both ratchet on every trade in the underlying. Counting them would make
    every quote look changed and defeat the filter entirely."""
    quotes = {"NFO|1": {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1, "oi": 10, "v": 100}}
    feed = _feed(["NFO|1"], quotes)
    w = TickWriter(str(tmp_path))
    tick_persist.snapshot_once(feed, w, _MIDSESSION)
    quotes["NFO|1"] = {**quotes["NFO|1"], "oi": 11, "v": 250}
    assert tick_persist.snapshot_once(feed, w, _MIDSESSION) == 0


def test_snapshot_survives_a_failing_feed(tmp_path):
    """The writer shares a process with quote serving, so it must never raise:
    a status() or get_quote() fault has to degrade to fewer rows."""
    bad = MagicMock()
    bad.status.side_effect = RuntimeError("feed down")
    assert tick_persist.snapshot_once(bad, TickWriter(str(tmp_path)), _MIDSESSION) == 0

    partial = MagicMock()
    partial.status.return_value = {"subscriptions": ["NFO|1", "NFO|2"]}
    partial.get_quote.side_effect = [RuntimeError("boom"), {"ts": "SYMB", "lp": 5.0}]
    assert tick_persist.snapshot_once(partial, TickWriter(str(tmp_path)), _MIDSESSION) == 1


def test_missing_or_empty_tick_is_skipped_not_written_blank(tmp_path):
    """An instrument subscribed but not yet ticking must produce no row — a
    blank row would read as a real observation of a zero price."""
    feed = _feed(["NFO|1", "NFO|2"], {"NFO|2": {"ts": "SYMB", "lp": 3.0}})
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path)), _MIDSESSION) == 1


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


def test_symbol_with_a_space_becomes_a_safe_filename(tmp_path):
    """Noren sends the index as "Nifty 50"; a path with a space breaks naive
    globbing downstream. The row keeps the true symbol, only the path is
    sanitised."""
    feed = _feed(["NSE|26000"], {"NSE|26000": {"ts": "Nifty 50", "lp": 24124.45}})
    assert tick_persist.snapshot_once(feed, TickWriter(str(tmp_path)), _MIDSESSION) == 1
    assert tick_persist.safe_name("Nifty 50") == "Nifty_50"
    assert _rows(tmp_path, "Nifty 50")[0]["symbol"] == "Nifty 50"


def _at(h, m):
    """Fri 2026-08-28 (a weekday) at the given IST time."""
    return _MIDSESSION.replace(hour=h, minute=m)


def test_nse_stops_heartbeating_after_its_own_close(tmp_path):
    """The proxy now runs to 23:58 for MCX. Without a per-exchange gate the
    ~200 NSE/NFO instruments would repeat a dead quote once a minute for the
    eight hours after 15:30 — ~96k identical rows a day on a 1GB box."""
    assert tick_persist.in_session("NFO|1", _at(14, 0)) is True
    assert tick_persist.in_session("NFO|1", _at(16, 0)) is False

    w = TickWriter(str(tmp_path))
    quote = {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1, "bq1": 1, "sq1": 1}
    assert w.should_write("NFO|1", quote, 0.0, False) is True          # first sight
    assert w.should_write("NFO|1", quote, 10_000.0, False) is False    # no heartbeat


def test_a_real_move_is_written_even_after_close(tmp_path):
    """Suppressing the heartbeat must not suppress a genuine post-close print —
    a settlement or an after-hours correction still belongs in the series."""
    w = TickWriter(str(tmp_path))
    base = {"ts": "SYMA", "lp": 1.0, "bp1": 0.9, "sp1": 1.1, "bq1": 1, "sq1": 1}
    assert w.should_write("NFO|1", base, 0.0, False) is True
    assert w.should_write("NFO|1", {**base, "lp": 2.0}, 1.0, False) is True


def test_mcx_still_heartbeats_through_the_evening(tmp_path):
    """MCX runs to 23:30 (23:55 on US-DST days) — the whole point of the
    extension is that its series stays sampled that long."""
    assert tick_persist.in_session("MCX|1", _at(20, 0)) is True
    assert tick_persist.in_session("MCX|1", _at(23, 40)) is True

    w = TickWriter(str(tmp_path))
    quote = {"ts": "GOLD", "lp": 1.0, "bp1": 0.9, "sp1": 1.1, "bq1": 1, "sq1": 1}
    assert w.should_write("MCX|1", quote, 0.0, True) is True
    assert w.should_write("MCX|1", quote, tick_persist.HEARTBEAT_SEC + 1, True) is True


def test_unknown_exchange_fails_open(tmp_path):
    """A segment we have no window for must degrade to redundant rows, never to
    a silent hole in the series."""
    assert tick_persist.in_session("BCD|1", _at(21, 0)) is True


def test_nothing_heartbeats_over_the_weekend(tmp_path):
    """The proxy does not auto-exit on a weekend, and the box now stays up — so
    a Saturday would otherwise write ~1.2M frozen rows."""
    saturday = dt.datetime(2026, 8, 29, 12, 0, tzinfo=tick_persist.IST)
    assert saturday.weekday() == 5
    assert tick_persist.in_session("MCX|1", saturday) is False
    assert tick_persist.in_session("BCD|1", saturday) is False


def test_mcx_trades_the_evening_on_an_nse_holiday(tmp_path):
    """The whole reason NSE's calendar cannot be reused for MCX: on eleven days
    a year NSE is shut and MCX closes only its morning, trading the evening as
    usual. Applying the NSE list to MCX would discard real commodity ticks —
    unrecoverable, unlike the frozen rows this gate exists to suppress."""
    ganesh_morning = dt.datetime(2026, 9, 14, 11, 0, tzinfo=tick_persist.IST)
    ganesh_evening = dt.datetime(2026, 9, 14, 20, 0, tzinfo=tick_persist.IST)
    assert ganesh_morning.weekday() < 5  # a Monday, not a weekend effect

    assert tick_persist.in_session("MCX|1", ganesh_morning) is False
    assert tick_persist.in_session("MCX|1", ganesh_evening) is True
    # NSE is shut for the whole day, both sides of 17:00.
    assert tick_persist.in_session("NFO|1", ganesh_morning) is False
    assert tick_persist.in_session("NFO|1", ganesh_evening) is False


def test_a_full_holiday_closes_both_exchanges(tmp_path):
    """Four days in 2026 close MCX's evening too — Republic Day, Good Friday,
    Gandhi Jayanti, Christmas."""
    for day in ("2026-10-02", "2026-12-25"):
        d = dt.date.fromisoformat(day)
        evening = dt.datetime(d.year, d.month, d.day, 20, 0, tzinfo=tick_persist.IST)
        assert tick_persist.in_session("MCX|1", evening) is False, day
        assert tick_persist.in_session("NFO|1", evening) is False, day


def test_muhurat_sunday_still_persists(tmp_path):
    """Diwali muhurat is the one session of the year that falls on a Sunday.
    The weekday check would otherwise discard it entirely."""
    muhurat = dt.datetime(2026, 11, 8, 18, 30, tzinfo=tick_persist.IST)
    assert muhurat.weekday() == 6
    assert tick_persist.in_session("NFO|1", muhurat) is True

    ordinary_sunday = dt.datetime(2026, 11, 15, 18, 30, tzinfo=tick_persist.IST)
    assert tick_persist.in_session("NFO|1", ordinary_sunday) is False


def test_an_uncovered_year_fails_open(tmp_path):
    """The calendars are hardcoded for 2026. When they run out the gate must
    persist everything and say so, not silently stop writing on every day of
    2027."""
    tick_persist._calendar_warned = False
    y2027 = dt.datetime(2027, 3, 10, 11, 0, tzinfo=tick_persist.IST)
    assert tick_persist.in_session("NFO|1", y2027) is True
    # Even at an hour that would be outside any session in a covered year.
    assert tick_persist.in_session("NFO|1", y2027.replace(hour=22)) is True
