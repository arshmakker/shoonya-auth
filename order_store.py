"""order_store.py — thread-safe cache of Noren WS order updates ('om').

The authenticated WS session carries order updates on the same socket as the
touchline feed. ws_feed.py dropped them for the feed's whole life ("order
updates ('om') share the socket and are ignored"), so every consumer learned an
order's fate by polling single_order_history / get_order_book instead.

That polling is what failed on 2026-09-01: a BUY 845 wing at LMT filled 650 and
rested. It was truthfully non-terminal in both REST sources, so the 45s poll
timed out, cancelled into the partial, and halted the session with an orphan
leg. The poller only ever inspects *status*, never fill quantity — a resting
order 650/845 filled is indistinguishable from one untouched. An 'om' frame
carries both, immediately.

Fields mirror Noren's order-update shape and are normalised the way tick_store
normalises touchlines: numbers become numbers, missing stays missing, and no
value is invented. Consumers keyed on 'norenordno'.
"""

import threading
import time

# Quantities and prices arrive as strings on the wire. 'fillshares' is the one
# that matters most here and the one the REST poller never reads.
_NUMERIC_FIELDS = {
    "qty": int,
    "fillshares": int,
    "avgprc": float,
    "prc": float,
    "trgprc": float,
}
# 'status'/'rpt' are both carried: Noren's per-report key is 'rpt' while
# 'status' is the order-level view, and live_order_manager._parse_status
# already treats rpt as authoritative with status as fallback. Keep both so a
# consumer can apply the same precedence it applies to REST history.
_PASSTHROUGH_FIELDS = (
    "norenordno",
    "status",
    "rpt",
    "tsym",
    "exch",
    "trantype",
    "prctyp",
    "rejreason",
    "norentm",
    "exch_tm",
    "remarks",
    "prd",
)

# Orders are bounded per session but the store must not grow without limit if
# the proxy runs for days. Oldest-first eviction, generous enough that a full
# trading day never evicts anything a consumer still cares about.
_MAX_ORDERS = 2000


class OrderStore:
    def __init__(self, max_orders=_MAX_ORDERS):
        self._lock = threading.Lock()
        self._orders = {}
        self._received_at = {}
        self._max_orders = max_orders
        self._update_count = 0

    def normalize(self, msg):
        """Map a Noren 'om' message to (order_no, record).

        Returns (None, {}) when the frame carries no order number — there is
        nothing to key on, and inventing a key would hide a real parse problem.
        """
        order_no = str(msg.get("norenordno", "") or "")
        if not order_no:
            return None, {}
        record = {}
        for field, caster in _NUMERIC_FIELDS.items():
            raw = msg.get(field)
            if raw in (None, ""):
                continue
            try:
                record[field] = caster(float(raw))
            except (TypeError, ValueError):
                continue
        for field in _PASSTHROUGH_FIELDS:
            value = msg.get(field)
            if value not in (None, ""):
                record[field] = value
        return order_no, record

    def update(self, order_no, record):
        """Merge a record over whatever is already known for this order.

        Merged rather than replaced: Noren emits several frames per order and a
        later one can omit a field an earlier one carried, so overwriting
        wholesale would lose the fill quantity a consumer is waiting on.
        """
        if not order_no:
            return
        with self._lock:
            self._update_count += 1
            existing = self._orders.get(order_no)
            if existing is None and len(self._orders) >= self._max_orders:
                oldest = min(self._received_at, key=self._received_at.get)
                self._orders.pop(oldest, None)
                self._received_at.pop(oldest, None)
            merged = dict(existing or {})
            merged.update(record)
            self._orders[order_no] = merged
            self._received_at[order_no] = time.time()

    def get(self, order_no):
        """Latest known state for one order, with the age of the last update,
        or None if nothing has been seen for it."""
        with self._lock:
            record = self._orders.get(str(order_no))
            if record is None:
                return None
            out = dict(record)
            out["_received_at"] = self._received_at.get(str(order_no))
            out["_age_sec"] = time.time() - self._received_at.get(str(order_no), time.time())
            return out

    def all(self):
        with self._lock:
            return {k: dict(v) for k, v in self._orders.items()}

    def stats(self):
        with self._lock:
            return {
                "orders_tracked": len(self._orders),
                "updates_received": self._update_count,
            }
