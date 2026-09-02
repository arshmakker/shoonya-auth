"""tests/test_shutdown_time.py — when the proxy ends its day.

SHOONYA_SHUTDOWN_TIME decides when _market_close_watchdog exits the process.
The default (15:40) is right for a session that only trades NSE/NFO; the
droplet overrides it to 23:58 because it captures the MCX evening. The
asymmetry is the whole point: a value that is merely *wrong* rather than
*malformed* costs eight hours of unrecoverable commodity ticks, so garbage
must be fatal instead of falling back to the default.
"""

import datetime as dt

import pytest

from broker_proxy import _DEFAULT_SHUTDOWN_TIME, _market_close_watchdog, _resolve_shutdown_time


# ── Resolution ───────────────────────────────────────────────────────────────

def test_resolve_when_unset_then_nse_close_buffer():
    """15:40 is a buffer past the 15:30 NSE close — correct for start.sh, which
    passes no SHOONYA_TICK_PERSIST_DIR and subscribes no MCX instruments."""
    assert _DEFAULT_SHUTDOWN_TIME == "15:40"
    for unset in (None, "", "   "):
        assert _resolve_shutdown_time(unset) == (15, 40)


def test_resolve_when_override_then_that_time():
    assert _resolve_shutdown_time("23:58") == (23, 58)   # start_vps.sh, MCX evening
    assert _resolve_shutdown_time(" 09:05 ") == (9, 5)
    assert _resolve_shutdown_time("0:00") == (0, 0)


@pytest.mark.parametrize(
    "bad", ["1540", "23-58", "25:00", "12:60", "-1:00", "abc", "12:", ":30", "12:30:45"]
)
def test_resolve_when_malformed_then_fatal_not_default(bad):
    """The failure mode this guards: a typo in start_vps.sh silently resolving
    to 15:40 and taking the whole MCX evening with it, invisibly."""
    with pytest.raises(SystemExit) as exc:
        _resolve_shutdown_time(bad)
    assert "SHOONYA_SHUTDOWN_TIME" in str(exc.value)


# ── Watchdog ─────────────────────────────────────────────────────────────────

def _freeze(monkeypatch, when: dt.datetime):
    import broker_proxy

    class _Clock:
        @staticmethod
        def now(tz=None):
            return when

    monkeypatch.setattr(broker_proxy, "datetime", _Clock)


class _Exited(Exception):
    """Stands in for os._exit, which never returns."""

    def __init__(self, code):
        self.code = code


def _exit_trap(monkeypatch):
    """Raise rather than record. A stub that merely returns lets the watchdog
    run on past the exit and reach a second one, which the real os._exit can
    never do — so a plain recorder would pass on code that exits twice."""
    import broker_proxy

    def _exit(code):
        raise _Exited(code)

    slept = []
    monkeypatch.setattr(broker_proxy.os, "_exit", _exit)
    monkeypatch.setattr(broker_proxy.time, "sleep", slept.append)
    return slept


def test_watchdog_when_started_after_target_then_exits_immediately(monkeypatch):
    _freeze(monkeypatch, dt.datetime(2026, 9, 2, 16, 0))          # Wednesday
    _exit_trap(monkeypatch)
    with pytest.raises(_Exited) as exc:
        _market_close_watchdog((15, 40))
    assert exc.value.code == 0


def test_watchdog_when_before_target_then_sleeps_then_exits(monkeypatch):
    _freeze(monkeypatch, dt.datetime(2026, 9, 2, 9, 30))          # Wednesday
    slept = _exit_trap(monkeypatch)
    with pytest.raises(_Exited) as exc:
        _market_close_watchdog((15, 40))
    assert exc.value.code == 0
    # 09:30 -> 15:40 is 6h10m. Asserting the DURATION, not just that an exit
    # happened: the target arithmetic is the part that differs between the
    # 15:40 default and the droplet's 23:58, so an exit-only assertion would
    # pass just as happily with the wrong time in force.
    assert slept == [6 * 3600 + 10 * 60]


def test_watchdog_when_mcx_override_then_waits_until_2358(monkeypatch):
    """The droplet's configuration, end to end: same 09:30 start, 23:58 target."""
    _freeze(monkeypatch, dt.datetime(2026, 9, 2, 9, 30))          # Wednesday
    slept = _exit_trap(monkeypatch)
    with pytest.raises(_Exited):
        _market_close_watchdog(_resolve_shutdown_time("23:58"))
    assert slept == [14 * 3600 + 28 * 60]


def test_watchdog_when_weekend_then_never_exits(monkeypatch):
    """A Friday-started proxy already exits Friday 23:58, so the weekend is
    covered; this branch only spares a Saturday start from an instant exit."""
    _freeze(monkeypatch, dt.datetime(2026, 9, 5, 16, 0))          # Saturday
    _exit_trap(monkeypatch)
    _market_close_watchdog((15, 40))   # returns; no _Exited raised
