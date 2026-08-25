"""tests/test_ws_feed.py — WSFeedManager behaviors (TDD/BDD).

The SDK's NorenApi.start_websocket auto-reconnects BUT its internal
__resubscribe() is commented out — after any reconnect, subscriptions are
silently LOST. The single most important behavior pinned here: subscriptions
survive reconnects because the manager re-sends them from its own set on
every socket-open callback.
"""

import time

import pytest

from ws_feed import WSFeedManager, parse_instruments_spec


class FakeApi:
    """Duck-typed ShoonyaApiPy stand-in: records wire calls, exposes callbacks."""

    def __init__(self):
        self.started = False
        self.closed = False
        self.sent_subscribes = []      # every subscribe() payload (list of instruments)
        self.sent_unsubscribes = []
        self.callbacks = {}

    def start_websocket(self, subscribe_callback=None, order_update_callback=None,
                        socket_open_callback=None, socket_close_callback=None,
                        socket_error_callback=None):
        self.started = True
        self.callbacks = {
            "tick": subscribe_callback,
            "open": socket_open_callback,
            "close": socket_close_callback,
            "error": socket_error_callback,
        }

    def close_websocket(self):
        self.closed = True

    def subscribe(self, instrument, feed_type=None):
        self.sent_subscribes.append(list(instrument) if isinstance(instrument, list) else instrument)

    def unsubscribe(self, instrument, feed_type=None):
        self.sent_unsubscribes.append(list(instrument) if isinstance(instrument, list) else instrument)

    # Test helpers -----------------------------------------------------------
    def open_socket(self):
        self.callbacks["open"]()

    def drop_socket(self):
        self.callbacks["close"]()

    def deliver(self, msg):
        self.callbacks["tick"](msg)


# ── Lifecycle ────────────────────────────────────────────────────────────────

def test_start_when_called_then_websocket_started_with_callbacks():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    assert api.started is True
    assert set(api.callbacks) == {"tick", "open", "close", "error"}


def test_stop_when_called_then_sdk_close_invoked():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    feed.stop()
    assert api.closed is True


# ── THE critical behavior: subscriptions survive reconnects ─────────────────

def test_resubscribe_when_socket_reopens_then_all_subscriptions_resent():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    feed.subscribe(["NSE|26000", "NFO|12345"])
    api.open_socket()
    assert api.sent_subscribes == [sorted(["NSE|26000", "NFO|12345"])]

    # Simulate mid-session drop + SDK auto-reconnect → on_open fires again.
    api.drop_socket()
    api.open_socket()
    assert api.sent_subscribes[-1] == sorted(["NSE|26000", "NFO|12345"]), \
        "SDK does NOT resubscribe on reconnect — manager must"


def test_subscribe_when_disconnected_then_queued_and_sent_on_open():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()          # not opened yet
    feed.subscribe(["NSE|26000"])
    assert api.sent_subscribes == []          # nothing sent while disconnected
    api.open_socket()
    assert api.sent_subscribes == [["NSE|26000"]]


def test_unsubscribe_when_connected_then_forwarded_and_removed_from_set():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    api.open_socket()
    feed.subscribe(["NSE|26000", "NFO|12345"])
    feed.unsubscribe(["NSE|26000"])
    assert api.sent_unsubscribes == [["NSE|26000"]]
    api.drop_socket()
    api.open_socket()
    assert api.sent_subscribes[-1] == ["NFO|12345"], "unsubscribed symbol must not come back"


# ── Tick routing ─────────────────────────────────────────────────────────────

def test_on_tick_when_touchline_then_normalized_quote_cached():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    api.deliver({"t": "tk", "e": "NSE", "tk": "26000", "lp": "24170.00"})

    q = feed.get_quote("NSE", "26000")
    assert q is not None and q["lp"] == 24170.00


def test_on_tick_when_non_feed_message_then_ignored():
    """Order updates ('om') and acks ('ak') ride the same socket — never cache them."""
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    api.deliver({"t": "om", "status": "COMPLETE"})
    api.deliver({"t": "ak", "s": "OK"})
    assert feed.status()["cached_ticks"] == 0


def test_on_tick_when_any_feed_message_then_last_msg_timestamp_advances():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    before = feed.status()["last_msg_age_sec"]
    time.sleep(0.01)
    api.deliver({"t": "tk", "e": "NSE", "tk": "26000", "lp": "1.0"})
    after = feed.status()["last_msg_age_sec"]
    assert after < before


# ── Quote reads ──────────────────────────────────────────────────────────────

def test_get_quote_when_stale_beyond_max_age_then_none():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    api.deliver({"t": "tk", "e": "NSE", "tk": "26000", "lp": "1.0"})
    feed._store._received_at["NSE|26000"] -= 120.0     # backdate 2 min
    assert feed.get_quote("NSE", "26000", max_age_sec=30.0) is None


def test_get_quote_when_unknown_symbol_then_none():
    feed = WSFeedManager(FakeApi())
    feed.start()
    assert feed.get_quote("NSE", "404") is None


# ── Status surface (backs GET /feed/status) ─────────────────────────────────

def test_status_when_running_then_reports_connection_health():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    s = feed.status()
    assert s["connected"] is False
    api.open_socket()
    api.deliver({"t": "tk", "e": "NSE", "tk": "26000", "lp": "1.0"})
    s = feed.status()
    assert s["connected"] is True
    assert s["subscriptions"] == []
    assert s["cached_ticks"] == 1
    assert 0 <= s["last_msg_age_sec"] < 5


def test_status_when_error_recorded_then_last_error_exposed():
    api = FakeApi()
    feed = WSFeedManager(api)
    feed.start()
    api.callbacks["error"]("boom")
    assert feed.status()["last_error"] == "boom"


# ── Instrument spec parsing (SHOONYA_WS_SUBSCRIBE env) ──────────────────────

def test_parse_spec_when_comma_separated_then_clean_list():
    assert parse_instruments_spec("NSE|26000, NFO|12345 ,MCX|9") == \
        ["NSE|26000", "NFO|12345", "MCX|9"]


def test_parse_spec_when_empty_or_junk_entries_then_dropped():
    assert parse_instruments_spec(", ,NSE|1,,") == ["NSE|1"]
    assert parse_instruments_spec("") == []
    assert parse_instruments_spec(None) == []


def test_parse_spec_when_missing_separator_then_entry_ignored():
    assert parse_instruments_spec("NSE26000,NSE|1") == ["NSE|1"]


def test_parse_spec_when_duplicates_then_deduped_first_position_kept():
    assert parse_instruments_spec("NSE|1,NFO|2,NSE|1") == ["NSE|1", "NFO|2"]
