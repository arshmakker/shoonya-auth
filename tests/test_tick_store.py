"""tests/test_tick_store.py — TickStore behaviors (TDD/BDD).

Scenario suite for the thread-safe tick cache that backs the WebSocket feed.
Field-shape contract: consumers (regimetrader market_data.get_ltp etc.) read
REST-quote fields lp / bp1 / sp1 / bq1 / sq1 / oi / v — normalized ticks MUST
use exactly those names with native numeric types.
"""

import threading
import time

import pytest

from tick_store import TickStore


# ── Normalization: WS touchline → REST-compatible quote shape ────────────────

def test_normalize_touchline_when_full_message_then_rest_compatible_fields():
    store = TickStore()
    ws_msg = {
        "t": "tk", "e": "NSE", "tk": "26000", "ts": "NIFTY",
        "lp": "24170.00", "pc": "-0.50", "v": "123456",
        "bp1": "24165.00", "sp1": "24175.00", "bq1": "100", "sq1": "200",
        "oi": "987654",
    }
    key, quote = store.normalize_touchline(ws_msg)

    assert key == "NSE|26000"
    # Numeric fields arrive as strings over WS; consumers expect numbers.
    assert quote["lp"] == 24170.00
    assert quote["bp1"] == 24165.00
    assert quote["sp1"] == 24175.00
    assert quote["bq1"] == 100
    assert quote["sq1"] == 200
    assert quote["oi"] == 987654
    assert quote["v"] == 123456
    assert quote["ts"] == "NIFTY"


def test_normalize_touchline_when_fields_missing_then_only_present_kept():
    """Early-session ticks may lack lp/bp1 — must not invent zeros."""
    store = TickStore()
    key, quote = store.normalize_touchline({"t": "tf", "e": "NFO", "tk": "12345"})
    assert key == "NFO|12345"
    assert "lp" not in quote
    assert "bp1" not in quote


def test_normalize_touchline_when_depth_message_then_same_shape():
    store = TickStore()
    key, _ = store.normalize_touchline(
        {"t": "dk", "e": "MCX", "tk": "9999", "lp": "60000.00"}
    )
    assert key == "MCX|9999"


# ── Cache semantics ──────────────────────────────────────────────────────────

def test_get_when_updated_then_returns_stored_quote():
    store = TickStore()
    key, quote = store.normalize_touchline({"t": "tk", "e": "NSE", "tk": "26000", "lp": "100.0"})
    store.update(key, quote)
    assert store.get("NSE|26000")["lp"] == 100.0


def test_get_when_never_updated_then_none():
    assert TickStore().get("NSE|1") is None


def test_get_when_older_than_max_age_then_stale_returns_none():
    store = TickStore()
    store.update("NSE|1", {"lp": 10.0})
    # Backdate the per-field received_at stamp.
    store._received_at["NSE|1"]["lp"] -= 60.0
    assert store.get("NSE|1", max_age_sec=30.0) is None


def test_get_when_lp_stale_but_depth_fresh_then_lp_dropped_not_whole_record():
    """Regression (2026-09-11): a depth-only delta (no 'lp') must not make a
    stale lp look fresh just because the key ticked. Staleness is tracked
    per field, so a stale lp is dropped while fresh depth fields survive."""
    store = TickStore()
    store.update("NSE|1", {"lp": 100.0, "bp1": 99.9, "sp1": 100.1})
    store._received_at["NSE|1"]["lp"] -= 60.0  # age lp by 60s
    store.update("NSE|1", {"bp1": 99.5, "sp1": 100.5})  # depth-only delta

    quote = store.get("NSE|1", max_age_sec=5.0)
    assert quote is not None
    assert "lp" not in quote, "stale lp must not be served as fresh"
    assert quote["bp1"] == 99.5
    assert quote["sp1"] == 100.5


def test_get_when_oi_stale_but_key_present_then_oi_still_returned():
    """Regression (2026-09-11): oi is cumulative — it only changes when a
    trade prints, so a perfectly liquid but momentarily quiet strike can go
    minutes without a fresh oi tick. Age-filtering it like lp/bp1/sp1 makes
    quote.get("oi", 0) read 0 for a liquid strike, which iron_condor.py's
    _leg_illiquid() (book.oi < IC_MIN_ENTRY_OI) then wrongly rejects as
    illiquid. oi/v/o/pc must survive the freshness filter that lp/bp1/sp1/
    bq1/sq1 are subject to."""
    store = TickStore()
    store.update("NSE|1", {"lp": 100.0, "oi": 50000, "v": 12345, "o": 99.0, "pc": -0.5})
    store._received_at["NSE|1"]["oi"] -= 60.0
    store._received_at["NSE|1"]["v"] -= 60.0
    store._received_at["NSE|1"]["o"] -= 60.0
    store._received_at["NSE|1"]["pc"] -= 60.0
    store.update("NSE|1", {"lp": 100.5})  # keeps lp itself fresh

    quote = store.get("NSE|1", max_age_sec=5.0)
    assert quote is not None
    assert quote["oi"] == 50000, "stale oi must still be served, not dropped"
    assert quote["v"] == 12345
    assert quote["o"] == 99.0
    assert quote["pc"] == -0.5
    assert quote["lp"] == 100.5


def test_get_when_price_fields_stale_then_still_dropped_despite_oi_exemption():
    """oi/v/o/pc are exempt from the age filter, but lp/bp1/sp1/bq1/sq1 must
    remain subject to it — the exemption must not widen to the whole quote."""
    store = TickStore()
    store.update("NSE|1", {"lp": 100.0, "bp1": 99.9, "sp1": 100.1, "bq1": 5, "sq1": 7, "oi": 50000})
    store._received_at["NSE|1"]["lp"] -= 60.0
    store._received_at["NSE|1"]["bp1"] -= 60.0
    store._received_at["NSE|1"]["sp1"] -= 60.0
    store._received_at["NSE|1"]["bq1"] -= 60.0
    store._received_at["NSE|1"]["sq1"] -= 60.0

    quote = store.get("NSE|1", max_age_sec=5.0)
    assert quote is not None
    assert "lp" not in quote
    assert "bp1" not in quote
    assert "sp1" not in quote
    assert "bq1" not in quote
    assert "sq1" not in quote
    assert quote["oi"] == 50000


def test_get_when_within_max_age_then_fresh_returns_quote():
    store = TickStore()
    store.update("NSE|1", {"lp": 10.0})
    assert store.get("NSE|1", max_age_sec=30.0)["lp"] == 10.0


def test_update_when_same_key_twice_then_latest_wins():
    store = TickStore()
    store.update("NSE|1", {"lp": 10.0})
    store.update("NSE|1", {"lp": 11.5})
    assert store.get("NSE|1")["lp"] == 11.5


def test_update_when_partial_delta_then_merged_not_replaced():
    """Noren 'tf' updates carry ONLY changed fields — a book-only delta must
    not wipe lp/oi from the previously cached full touchline."""
    store = TickStore()
    store.update("NSE|1", {"lp": 32.65, "bp1": 32.55, "sp1": 32.75, "oi": 447460})
    store.update("NSE|1", {"bp1": 32.50, "bq1": 65})          # tf delta: no lp
    q = store.get("NSE|1")
    assert q["lp"] == 32.65, "delta must not erase last price"
    assert q["oi"] == 447460
    assert q["bp1"] == 32.50, "delta field must overwrite"
    assert q["sp1"] == 32.75


def test_update_when_delta_for_unknown_key_then_stored_as_is():
    store = TickStore()
    store.update("NSE|9", {"bp1": 1.0})
    assert store.get("NSE|9") == {"bp1": 1.0}


def test_keys_when_mixed_updates_then_all_instrument_keys_listed():
    store = TickStore()
    store.update("NSE|1", {"lp": 1})
    store.update("NFO|2", {"lp": 2})
    assert set(store.keys()) == {"NSE|1", "NFO|2"}


# ── Thread safety: concurrent writers must not corrupt state ────────────────

def test_update_when_concurrent_writers_then_no_exception_and_consistent():
    store = TickStore()

    def writer(i):
        for n in range(200):
            store.update(f"NSE|{i}", {"lp": float(n)})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(store.keys()) == 8
    for i in range(8):
        lp = store.get(f"NSE|{i}")["lp"]
        assert 0.0 <= lp <= 199.0


# ── Depth price/quantity pairing (2026-10-06) ────────────────────────────────
# Noren's touchline sends only CHANGED fields. A resting wing whose best
# bid/ask price is unchanged but whose size ticks sends bq1/sq1 and no bp1/sp1,
# so the per-field stamp on the price ages out after max_age_sec even though
# the book is live. A fresh size tick on a side confirms that side's price is
# still the best price (a price move would have been sent as bp1/sp1).

def _age(store, key, *fields, by=60.0):
    for f in fields:
        store._received_at[key][f] -= by


def test_get_when_bid_price_stale_but_bid_qty_fresh_then_bid_price_kept():
    store = TickStore()
    store.update("NFO|1", {"lp": 8.1, "bp1": 8.05, "bq1": 5000, "sp1": 8.15, "sq1": 4000})
    _age(store, "NFO|1", "bp1", "sp1", "sq1")
    store.update("NFO|1", {"bq1": 5200})  # size-only delta on the bid

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert quote["bp1"] == 8.05, "fresh bq1 must keep the unchanged bp1 alive"
    assert quote["bq1"] == 5200


def test_get_when_ask_price_stale_but_ask_qty_fresh_then_ask_price_kept():
    store = TickStore()
    store.update("NFO|1", {"lp": 8.1, "bp1": 8.05, "bq1": 5000, "sp1": 8.15, "sq1": 4000})
    _age(store, "NFO|1", "bp1", "bq1", "sp1")
    store.update("NFO|1", {"sq1": 4200})

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert quote["sp1"] == 8.15
    assert "bp1" not in quote, "bid side had no fresh tick; bid price must still age out"


def test_get_when_price_and_qty_both_stale_then_price_dropped():
    """Frozen feed: nothing ticks on the side, so the price must age out."""
    store = TickStore()
    store.update("NFO|1", {"lp": 8.1, "bp1": 8.05, "bq1": 5000, "sp1": 8.15, "sq1": 4000})
    _age(store, "NFO|1", "bp1", "bq1", "sp1", "sq1")

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert quote is None or ("bp1" not in quote and "sp1" not in quote)


def test_get_when_bid_qty_fresh_but_zero_then_stale_bid_price_not_revived():
    """An emptied side (bq1=0) must not make an old bp1 look like a live,
    tradable bid."""
    store = TickStore()
    store.update("NFO|1", {"lp": 8.1, "bp1": 8.05, "bq1": 5000})
    _age(store, "NFO|1", "bp1", "bq1")
    store.update("NFO|1", {"bq1": 0})

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert "bp1" not in quote


def test_get_when_qty_fresh_on_other_side_then_price_not_revived():
    """bq1 vouches for bp1 only, never sp1 (and vice versa)."""
    store = TickStore()
    store.update("NFO|1", {"bp1": 8.05, "bq1": 5000, "sp1": 8.15, "sq1": 4000})
    _age(store, "NFO|1", "bp1", "sp1", "sq1")
    store.update("NFO|1", {"bq1": 5100})

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert quote["bp1"] == 8.05
    assert "sp1" not in quote


def test_get_when_bid_qty_fresh_but_no_price_ever_seen_then_no_price_invented():
    store = TickStore()
    store.update("NFO|1", {"lp": 8.1, "bq1": 5000})
    quote = store.get("NFO|1", max_age_sec=5.0)
    assert "bp1" not in quote


# ── lp vouched by rising volume (2026-10-06) ─────────────────────────────────
# A trade at an UNCHANGED price sends a higher cumulative v but no lp (Noren
# sends changed fields only). lp then ages out of the 5s window although the
# market is trading at that price, quote_bridge sees no lp and falls back to
# REST (369 fallbacks in 2h on 2026-10-06; sp:rest on the 09:30:40 IST dip).
# A v that actually INCREASED proves a trade printed; had the price moved, lp
# would have been sent. An unchanged v proves nothing.

def test_get_when_lp_stale_but_volume_rose_recently_then_lp_kept():
    store = TickStore()
    store.update("NFO|1", {"lp": 25.0, "v": 1000})
    _age(store, "NFO|1", "lp", "v")
    store.update("NFO|1", {"v": 1500})  # trade at unchanged price

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert quote["lp"] == 25.0


def test_get_when_lp_stale_and_volume_unchanged_then_lp_dropped():
    store = TickStore()
    store.update("NFO|1", {"lp": 25.0, "v": 1000})
    _age(store, "NFO|1", "lp", "v")
    store.update("NFO|1", {"v": 1000, "bq1": 5})  # same v re-sent, no trade

    quote = store.get("NFO|1", max_age_sec=5.0)
    assert "lp" not in quote


def test_get_when_lp_stale_and_volume_fell_then_lp_dropped():
    """v is cumulative; a decrease is a reset/glitch, not a trade."""
    store = TickStore()
    store.update("NFO|1", {"lp": 25.0, "v": 1000})
    _age(store, "NFO|1", "lp", "v")
    store.update("NFO|1", {"v": 400})

    assert "lp" not in store.get("NFO|1", max_age_sec=5.0)


def test_get_when_lp_stale_and_volume_rise_itself_stale_then_lp_dropped():
    store = TickStore()
    store.update("NFO|1", {"lp": 25.0, "v": 1000})
    store.update("NFO|1", {"v": 1500})
    _age(store, "NFO|1", "lp", "v")
    store._v_rose_at["NFO|1"] -= 60.0  # the rise happened a minute ago

    assert "lp" not in store.get("NFO|1", max_age_sec=5.0)


def test_get_when_first_ever_volume_seen_then_it_is_not_a_rise():
    """No prior v to compare to: cannot vouch for a stale lp."""
    store = TickStore()
    store.update("NFO|1", {"lp": 25.0})
    _age(store, "NFO|1", "lp")
    store.update("NFO|1", {"v": 1000})

    assert "lp" not in store.get("NFO|1", max_age_sec=5.0)


def test_get_when_volume_rose_but_no_lp_ever_seen_then_no_lp_invented():
    store = TickStore()
    store.update("NFO|1", {"v": 1000})
    store.update("NFO|1", {"v": 1500})
    quote = store.get("NFO|1", max_age_sec=5.0)
    assert "lp" not in quote
