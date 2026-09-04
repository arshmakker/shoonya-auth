"""ws_feed.py — feed manager over our own WsClient transport.

'Connected' means the broker's 'ak OK' ack was observed on the wire.
Subscriptions are sent only after that ack and re-sent after every
reconnect+ack cycle.

Order updates ('om') ride the same authenticated socket. They were dropped for
this feed's whole life until 2026-09-01, which left every consumer polling REST
for an order's fate — and that polling is what halted the session that day (a
845-qty wing filled 650 and rested; truthfully non-terminal in both REST
sources, so the 45s poll timed out and cancelled into the partial). An 'om'
frame carries status AND fillshares immediately, so they are now captured into
an OrderStore. Message types that are neither feed nor order updates are
counted and logged once per type, so "we are silently discarding something"
can never again be invisible.
"""

import json
import logging
import threading
import time

from order_store import OrderStore
from tick_store import TickStore
from ws_client import WsClient

log = logging.getLogger("ws_feed")

_FEED_MESSAGE_TYPES = frozenset({"tk", "tf", "dk", "df"})
_ORDER_MESSAGE_TYPE = "om"
_NEVER_RECEIVED_AGE_SEC = 1e9
_VALID_MODES = frozenset({"rest", "shadow", "hybrid"})


def normalize_mode(raw):
    mode = str(raw or "").strip().lower()
    return mode if mode in _VALID_MODES else "rest"


def cache_serving_for(mode):
    return normalize_mode(mode) == "hybrid"


def validator_runs_for(mode):
    return normalize_mode(mode) in ("shadow", "hybrid")


def parse_instruments_spec(raw):
    """Parse 'NSE|26000,NFO|123' style specs: strip, drop junk, dedupe."""
    if not raw:
        return []
    seen = []
    for entry in str(raw).split(","):
        entry = entry.strip()
        if "|" in entry and entry not in seen:
            seen.append(entry)
    return seen


class WSFeedManager:
    def __init__(self, access_token, uid, default_max_age_sec=10.0, transport_factory=None):
        self._store = TickStore()
        self._orders = OrderStore()
        self._uid = uid
        # Per-type tally of frames this feed does not consume. The 'om' drop
        # went unnoticed for months because unknown types vanished silently;
        # counting them makes the next one self-reporting.
        self._unhandled_types = {}
        self._subscriptions = set()
        self._lock = threading.Lock()
        self._connected = False
        self._last_msg_monotonic = None
        self._last_error = None
        self._default_max_age_sec = default_max_age_sec
        factory = transport_factory or (
            lambda: WsClient(access_token=access_token, uid=uid, on_message=self._on_raw)
        )
        self._transport = factory()

    def start(self):
        self._transport.start()
        log.info("WS feed manager started")

    def stop(self):
        self._transport.close()

    def subscribe(self, instruments):
        with self._lock:
            self._subscriptions.update(instruments)
        if self._connected:
            self._send_touchline("t", list(instruments))

    def unsubscribe(self, instruments):
        with self._lock:
            self._subscriptions.difference_update(instruments)
        if self._connected:
            self._send_touchline("u", list(instruments))

    def get_order(self, order_no):
        return self._orders.get(order_no)

    def all_orders(self):
        return self._orders.all()

    def get_quote(self, exchange, token, max_age_sec=None):
        effective_age = self._default_max_age_sec if max_age_sec is None else max_age_sec
        return self._store.get(f"{exchange}|{token}", max_age_sec=effective_age)

    def status(self):
        with self._lock:
            subscriptions = sorted(self._subscriptions)
        if self._last_msg_monotonic is None:
            last_msg_age = _NEVER_RECEIVED_AGE_SEC
        else:
            last_msg_age = time.monotonic() - self._last_msg_monotonic
        return {
            "connected": self._connected,
            "subscriptions": subscriptions,
            "cached_ticks": len(self._store.keys()),
            "last_msg_age_sec": round(last_msg_age, 3),
            "last_error": self._last_error,
            "unhandled_msg_types": dict(self._unhandled_types),
            **self._orders.stats(),
        }

    def _send_touchline(self, kind, instruments):
        if not instruments:
            return
        payload = json.dumps({"t": kind, "k": "#".join(sorted(instruments))})
        self._transport.send(payload)

    def _resubscribe_all(self):
        with self._lock:
            snapshot = sorted(self._subscriptions)
        if snapshot:
            log.info("ack OK — subscribing %d instruments", len(snapshot))
            self._send_touchline("t", snapshot)
        # Order updates ('om') require this explicit subscription — the broker
        # never pushes them just because the socket is authenticated. Must be
        # re-sent on every reconnect, same as the touchline subscriptions above.
        self._transport.send(json.dumps({"t": "o", "actid": self._uid}))
        log.info("ack OK — subscribed to order updates (actid=%s)", self._uid)

    def _on_raw(self, msg):
        t = msg.get("t")
        if t == "ak":
            if msg.get("s") == "OK":
                self._connected = True
                self._last_error = None
                self._resubscribe_all()
            else:
                self._last_error = f"auth ack rejected: {msg}"
                log.error(self._last_error)
            return
        if t in _FEED_MESSAGE_TYPES:
            key, quote = self._store.normalize_touchline(msg)
            # Skip only when BOTH exchange and token are absent — matches the
            # "|"-sentinel check this replaces exactly (a key with just one
            # side present, e.g. "NFO|", still updates as it did before).
            if msg.get("e") or msg.get("tk"):
                self._store.update(key, quote)
            self._last_msg_monotonic = time.monotonic()
            return
        if t == _ORDER_MESSAGE_TYPE:
            order_no, record = self._orders.normalize(msg)
            if order_no:
                self._orders.update(order_no, record)
                log.info(
                    "order update %s %s status=%s rpt=%s fill=%s/%s",
                    order_no,
                    record.get("tsym", ""),
                    record.get("status", ""),
                    record.get("rpt", ""),
                    record.get("fillshares", ""),
                    record.get("qty", ""),
                )
            else:
                log.warning("order update with no norenordno — dropped: %s", msg)
            self._last_msg_monotonic = time.monotonic()
            return
        # Anything else: count it, and say so the first time each type appears.
        count = self._unhandled_types.get(t, 0) + 1
        self._unhandled_types[t] = count
        if count == 1:
            log.info("unhandled WS message type %r (first occurrence): %s", t, msg)
