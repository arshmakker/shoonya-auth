"""tests/test_ws_feed.py — WSFeedManager behaviors (own transport, TDD/BDD).

The manager no longer uses NorenApi.start_websocket (silently unack'd in the
proxy context). It drives a thin WsClient transport with the session token
from the existing login flow. 'Connected' means the broker's 'ak OK' ack was
SEEN — subscriptions are (re)sent only after that ack, which also preserves
them across reconnects (the SDK never resubscribed).
"""

import json

import pytest

from ws_feed import WSFeedManager
from ws_client import next_reconnect_delay


class FakeTransport:
    def __init__(self, on_message=None):
        self.on_message = on_message
        self.started = False
        self.closed = False
        self.sent = []

    def start(self):
        self.started = True

    def send(self, text):
        self.sent.append(text)

    def close(self):
        self.closed = True

    def fire_open(self):
        pass  # handshake is internal to WsClient; ack drives state instead

    def fire_text(self, obj):
        self.on_message(obj)

    def fire_close(self):
        pass

    def fire_error(self, err):
        if self.on_transport_error:
            self.on_transport_error(err)

    on_transport_error = None


def make_feed():
    transport = FakeTransport()
    feed = WSFeedManager(
        access_token="tok123", uid="U1", transport_factory=lambda: transport
    )
    transport.on_message = feed._on_raw
    return feed, transport


# ── Lifecycle ────────────────────────────────────────────────────────────────

def test_start_when_called_then_transport_started():
    feed, transport = make_feed()
    feed.start()
    assert transport.started is True


def test_stop_when_called_then_transport_closed():
    feed, transport = make_feed()
    feed.start()
    feed.stop()
    assert transport.closed is True


# ── Auth ack gating ──────────────────────────────────────────────────────────

def test_ack_ok_when_received_then_connected_and_subscriptions_resent():
    feed, transport = make_feed()
    feed.start()
    feed.subscribe(["NSE|26000", "NFO|12345"])
    assert transport.sent == [], "nothing may be sent before the broker acks"

    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK", "uid": "U1"})

    assert feed.status()["connected"] is True
    assert len(transport.sent) == 1
    sent = json.loads(transport.sent[0])
    assert sent["t"] == "t"
    assert sorted(sent["k"].split("#")) == ["NFO|12345", "NSE|26000"]


def test_resubscribe_when_reconnected_and_acked_then_subscriptions_resent_again():
    """A fresh ack after any reconnect must restore the full subscription set."""
    feed, transport = make_feed()
    feed.start()
    feed.subscribe(["NSE|26000"])
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})

    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})

    assert feed.status()["connected"] is True
    kinds = [json.loads(s)["t"] for s in transport.sent]
    assert kinds == ["t", "t"], "one full subscribe per ack cycle"
    assert json.loads(transport.sent[-1])["k"] == "NSE|26000"


def test_ack_not_ok_when_received_then_error_recorded_stays_disconnected():
    feed, transport = make_feed()
    feed.start()
    feed.subscribe(["NSE|26000"])
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "Not_Ok"})

    assert feed.status()["connected"] is False
    assert "Not_Ok" in (feed.status()["last_error"] or "")
    assert transport.sent == [], "never send subscriptions without a valid ack"


def test_subscribe_when_already_acked_then_sent_immediately():
    feed, transport = make_feed()
    feed.start()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    transport.sent.clear()

    feed.subscribe(["NSE|26000"])
    assert json.loads(transport.sent[0])["k"] == "NSE|26000"


def test_unsubscribe_when_acked_then_forwarded_and_removed_from_set():
    feed, transport = make_feed()
    feed.start()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    feed.subscribe(["NSE|26000", "NFO|12345"])

    feed.unsubscribe(["NSE|26000"])

    kinds = [json.loads(s)["t"] for s in transport.sent]
    assert kinds[-1] == "u"
    transport.fire_close()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    assert json.loads(transport.sent[-1])["k"] == "NFO|12345"


# ── Tick routing ─────────────────────────────────────────────────────────────

def test_on_message_when_touchline_then_normalized_quote_cached():
    feed, transport = make_feed()
    feed.start()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    transport.fire_text({"t": "tk", "e": "NSE", "tk": "26000", "lp": "24170.00"})

    q = feed.get_quote("NSE", "26000")
    assert q is not None and q["lp"] == 24170.00


def test_on_message_when_order_update_then_ignored():
    feed, transport = make_feed()
    feed.start()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    transport.fire_text({"t": "om", "status": "COMPLETE"})
    assert feed.status()["cached_ticks"] == 0


# ── Quote reads ──────────────────────────────────────────────────────────────

def test_get_quote_when_stale_beyond_max_age_then_none():
    feed, transport = make_feed()
    feed.start()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    transport.fire_text({"t": "tk", "e": "NSE", "tk": "26000", "lp": "1.0"})
    feed._store._received_at["NSE|26000"] -= 120.0
    assert feed.get_quote("NSE", "26000", max_age_sec=30.0) is None


def test_get_quote_when_unknown_symbol_then_none():
    feed, _ = make_feed()
    feed.start()
    assert feed.get_quote("NSE", "404") is None


# ── Status surface ───────────────────────────────────────────────────────────

def test_status_when_acked_then_reports_health():
    feed, transport = make_feed()
    feed.start()
    transport.fire_open()
    transport.fire_text({"t": "ak", "s": "OK"})
    transport.fire_text({"t": "tk", "e": "NSE", "tk": "26000", "lp": "1.0"})

    s = feed.status()
    assert s["connected"] is True
    assert s["cached_ticks"] == 1
    assert 0 <= s["last_msg_age_sec"] < 5


# ── Reconnect backoff ────────────────────────────────────────────────────────

def test_backoff_when_short_lived_connection_then_doubles():
    assert next_reconnect_delay(uptime_sec=5, prev_delay=1.0) == 2.0
    assert next_reconnect_delay(uptime_sec=5, prev_delay=2.0) == 4.0


def test_backoff_when_capped_then_never_exceeds_60s():
    assert next_reconnect_delay(uptime_sec=3, prev_delay=60.0) == 60.0


def test_backoff_when_connection_was_healthy_then_resets_to_base():
    assert next_reconnect_delay(uptime_sec=300, prev_delay=32.0) == 1.0
