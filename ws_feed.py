"""ws_feed.py — feed manager over our own WsClient transport.

'Connected' means the broker's 'ak OK' ack was observed on the wire.
Subscriptions are sent only after that ack and re-sent after every
reconnect+ack cycle; order updates ('om') share the socket and are ignored.
"""

import json
import logging
import threading
import time

from tick_store import TickStore
from ws_client import WsClient

log = logging.getLogger("ws_feed")

_FEED_MESSAGE_TYPES = frozenset({"tk", "tf", "dk", "df"})
_NEVER_RECEIVED_AGE_SEC = 1e9


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
            if key != "|":
                self._store.update(key, quote)
            self._last_msg_monotonic = time.monotonic()
