"""tick_store.py — thread-safe tick cache normalizing WS touchlines into the
REST-quote field shape consumers already parse (lp/bp1/sp1/bq1/sq1/oi/v).
"""

import threading
import time

_FLOAT_FIELDS = ("lp", "bp1", "sp1", "pc")
_INT_FIELDS = ("bq1", "sq1", "oi", "v")
_PASSTHROUGH_FIELDS = ("ts", "e", "tk")


class TickStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._ticks = {}
        self._received_at = {}

    def normalize_touchline(self, msg):
        """Map an Noren WS touchline/depth message to (instrument_key, quote).

        WS fields arrive as strings; consumers expect native numbers.
        Missing fields stay missing — no invented zeros.
        """
        exchange = str(msg.get("e", ""))
        token = str(msg.get("tk", ""))
        key = f"{exchange}|{token}"

        quote = {}
        for field in _FLOAT_FIELDS:
            value = msg.get(field)
            if value not in (None, ""):
                try:
                    quote[field] = float(value)
                except (TypeError, ValueError):
                    pass
        for field in _INT_FIELDS:
            value = msg.get(field)
            if value not in (None, ""):
                try:
                    quote[field] = int(float(value))
                except (TypeError, ValueError):
                    pass
        for field in _PASSTHROUGH_FIELDS:
            if field in msg:
                quote[field] = msg[field]
        return key, quote

    def update(self, key, fields):
        with self._lock:
            existing = self._ticks.get(key)
            if existing is not None:
                existing.update(fields)
            else:
                self._ticks[key] = dict(fields)
            self._received_at[key] = time.monotonic()

    def get(self, key, max_age_sec=None):
        with self._lock:
            quote = self._ticks.get(key)
            if quote is None:
                return None
            if max_age_sec is not None:
                age = time.monotonic() - self._received_at.get(key, 0.0)
                if age > max_age_sec:
                    return None
            return dict(quote)

    def keys(self):
        with self._lock:
            return list(self._ticks.keys())
