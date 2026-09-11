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
