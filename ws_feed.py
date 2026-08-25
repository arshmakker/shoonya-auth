"""ws_feed.py — WebSocket feed manager owning the SDK's single WS connection.

The SDK auto-reconnects but does NOT resubscribe (its internal __resubscribe()
is commented out), so this manager keeps the subscription set and re-sends it
on every socket-open callback. Order updates ('om') and acks ('ak') share the
socket and are ignored here.
"""

import logging
import threading
import time

from tick_store import TickStore

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
    def __init__(self, api, default_max_age_sec=10.0):
        self._api = api
        self._store = TickStore()
        self._subscriptions = set()
        self._lock = threading.Lock()
        self._connected = False
        self._last_msg_monotonic = None
        self._last_error = None
        self._default_max_age_sec = default_max_age_sec

    def start(self):
        self._api.start_websocket(
            subscribe_callback=self._on_tick,
            socket_open_callback=self._on_open,
            socket_close_callback=self._on_close,
            socket_error_callback=self._on_error,
        )
        log.info("WS feed manager started")

    def stop(self):
        self._api.close_websocket()

    def subscribe(self, instruments):
        with self._lock:
            fresh = [i for i in instruments if i not in self._subscriptions]
            self._subscriptions.update(instruments)
        if self._connected and fresh:
            self._api.subscribe(fresh)

    def unsubscribe(self, instruments):
        with self._lock:
            self._subscriptions.difference_update(instruments)
        if self._connected:
            self._api.unsubscribe(list(instruments))

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

    def _on_open(self):
        self._connected = True
        with self._lock:
            snapshot = sorted(self._subscriptions)
        if snapshot:
            log.info("WS connected — resubscribing %d instruments", len(snapshot))
            self._api.subscribe(snapshot)

    def _on_close(self):
        self._connected = False
        log.warning("WS disconnected — SDK will auto-reconnect; subscriptions will be resent on reopen")

    def _on_error(self, error):
        self._last_error = str(error)
        log.error("WS error: %s", error)

    def _on_tick(self, msg):
        if not isinstance(msg, dict) or msg.get("t") not in _FEED_MESSAGE_TYPES:
            return
        key, quote = self._store.normalize_touchline(msg)
        if key != "|":
            self._store.update(key, quote)
        self._last_msg_monotonic = time.monotonic()
