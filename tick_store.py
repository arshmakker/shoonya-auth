"""tick_store.py — thread-safe tick cache normalizing WS touchlines into the
REST-quote field shape consumers already parse (lp/bp1/sp1/bq1/sq1/oi/v).
"""

import threading
import time

# 'o' (session open) is whitelisted alongside the traded/quoted prices: without
# it a WS-cached quote can never answer "what did this instrument open at",
# which is what day classification needs (2026-08-26). 'h'/'l'/'c' stay out
# until something needs them — and note Noren's 'c' is the PREVIOUS close, not
# today's, so it is not a drop-in for a close price.
_NUMERIC_FIELDS = {
    "lp": float,
    "bp1": float,
    "sp1": float,
    "pc": float,
    "o": float,
    "bq1": int,
    "sq1": int,
    "oi": int,
    "v": int,
}
# 'ts' is Noren's TRADING SYMBOL, not a timestamp; 'ft' is the feed time.
# Both are kept: consumers need the name to label data and the feed clock to
# tell a fresh quote from a stale cache entry.
_PASSTHROUGH_FIELDS = ("ts", "e", "tk", "ft")


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
        for field, cast in _NUMERIC_FIELDS.items():
            value = msg.get(field)
            if value not in (None, ""):
                try:
                    quote[field] = cast(float(value))
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
            # Stamped per field, not per key: a depth-only delta (Noren 'tf',
            # no 'lp') must not make an untouched, stale 'lp' look fresh just
            # because something else on this instrument ticked (2026-09-11).
            stamps = self._received_at.setdefault(key, {})
            now = time.monotonic()
            for field in fields:
                stamps[field] = now

    def get(self, key, max_age_sec=None):
        with self._lock:
            quote = self._ticks.get(key)
            if quote is None:
                return None
            if max_age_sec is None:
                return dict(quote)
            stamps = self._received_at.get(key, {})
            now = time.monotonic()
            fresh = {
                field: value
                for field, value in quote.items()
                if now - stamps.get(field, 0.0) <= max_age_sec
            }
            return fresh or None

    def keys(self):
        with self._lock:
            return list(self._ticks.keys())
