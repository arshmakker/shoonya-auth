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


# ── Order updates ('om') ─────────────────────────────────────────────────────
# Regression cover for 2026-09-01: 'om' frames rode this socket and were
# dropped, so consumers polled REST for order state. A BUY 845 wing filled 650
# and rested — truthfully non-terminal in single_order_history AND
# get_order_book, since neither distinguishes a partly-filled resting order
# from an untouched one. The 45s poll timed out, cancelled into the partial,
# and halted the session. The 'om' frame carries fillshares directly.

def _ack(transport):
    transport.fire_text({"t": "ak", "s": "OK"})


def test_order_update_is_captured():
    feed, transport = make_feed()
    _ack(transport)
    transport.fire_text(
        {
            "t": "om",
            "norenordno": "26090100361927",
            "tsym": "NIFTY15SEP26C24900",
            "status": "OPEN",
            "rpt": "NewAck",
            "qty": "845",
            "fillshares": "650",
            "trantype": "B",
        }
    )
    rec = feed.get_order("26090100361927")
    assert rec is not None
    assert rec["fillshares"] == 650 and rec["qty"] == 845
    assert rec["tsym"] == "NIFTY15SEP26C24900"
    assert rec["rpt"] == "NewAck"


def test_partial_fill_is_visible_while_status_still_open():
    """The whole point: status alone cannot tell 650/845 from 0/845."""
    feed, transport = make_feed()
    _ack(transport)
    for filled in ("0", "650"):
        transport.fire_text(
            {"t": "om", "norenordno": "X1", "status": "OPEN", "qty": "845", "fillshares": filled}
        )
    rec = feed.get_order("X1")
    assert rec["status"] == "OPEN"
    assert rec["fillshares"] == 650, "partial fill must be readable without a terminal status"


def test_order_updates_merge_rather_than_replace():
    """Noren emits several frames per order and a later one may omit a field an
    earlier one carried; overwriting wholesale would lose the fill quantity."""
    feed, transport = make_feed()
    _ack(transport)
    transport.fire_text({"t": "om", "norenordno": "X2", "qty": "845", "fillshares": "650"})
    transport.fire_text({"t": "om", "norenordno": "X2", "status": "COMPLETE"})
    rec = feed.get_order("X2")
    assert rec["status"] == "COMPLETE"
    assert rec["fillshares"] == 650, "earlier fill quantity must survive a later partial frame"


def test_order_update_without_order_number_is_dropped():
    feed, transport = make_feed()
    _ack(transport)
    transport.fire_text({"t": "om", "status": "COMPLETE"})
    assert feed.all_orders() == {}


def test_order_updates_do_not_pollute_the_tick_cache():
    feed, transport = make_feed()
    _ack(transport)
    transport.fire_text({"t": "om", "norenordno": "X3", "status": "COMPLETE"})
    assert feed.status()["cached_ticks"] == 0


def test_unhandled_message_types_are_counted():
    """The 'om' drop went unnoticed because unknown frames vanished silently."""
    feed, transport = make_feed()
    _ack(transport)
    transport.fire_text({"t": "zz"})
    transport.fire_text({"t": "zz"})
    assert feed.status()["unhandled_msg_types"] == {"zz": 2}


def test_feed_status_reports_order_counters():
    feed, transport = make_feed()
    _ack(transport)
    transport.fire_text({"t": "om", "norenordno": "X4", "status": "COMPLETE"})
    st = feed.status()
    assert st["orders_tracked"] == 1 and st["updates_received"] == 1


def test_unknown_order_returns_none():
    """404 at the proxy means 'no update seen', never 'order does not exist'."""
    feed, _ = make_feed()
    assert feed.get_order("nope") is None
