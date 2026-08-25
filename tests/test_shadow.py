"""tests/test_shadow.py — ShadowValidator behaviors (TDD/BDD).

Shadow mode runs the WS feed purely as an OBSERVER: consumers keep getting
REST quotes while the validator continuously compares WS ticks against fresh
REST quotes and logs divergences. This is the evidence gate before enabling
cache-first serving in production.
"""

import pytest

from shadow import ShadowValidator


class FakeApi:
    def __init__(self, quotes=None, error=None):
        self.quotes = quotes or {}
        self.error = error

    def get_quotes(self, exchange, token, *args, **kwargs):
        if self.error:
            raise self.error
        return self.quotes.get((exchange, token))


class FakeFeed:
    def __init__(self, ticks=None):
        self.ticks = ticks or {}

    def get_quote(self, exchange, token, max_age_sec=None):
        return self.ticks.get(f"{exchange}|{token}")


# ── Verdicts ─────────────────────────────────────────────────────────────────

def test_compare_when_prices_within_tolerance_then_match():
    api = FakeApi({("NSE", "26000"): {"lp": "100.00"}})
    feed = FakeFeed({"NSE|26000": {"lp": 100.05}})
    out = ShadowValidator(api, feed, divergence_tol_pct=1.0).compare("NSE", "26000")
    assert out["verdict"] == "match"
    assert out["delta_pct"] <= 1.0


def test_compare_when_ws_diverges_beyond_tolerance_then_diverge():
    api = FakeApi({("NFO", "123"): {"lp": "16.20"}})
    feed = FakeFeed({"NFO|123": {"lp": 16.80}})
    out = ShadowValidator(api, feed, divergence_tol_pct=1.0).compare("NFO", "123")
    assert out["verdict"] == "diverge"
    assert out["delta_pct"] > 1.0


def test_compare_when_rest_returns_spot_like_contamination_then_caught_as_diverge():
    """The known live quirk: REST sometimes returns ~spot (24170) for an option
    priced ~16. Shadow must FLAG it, not crash on the huge delta."""
    api = FakeApi({("NFO", "123"): {"lp": "24170.00"}})
    feed = FakeFeed({"NFO|123": {"lp": 16.20}})
    out = ShadowValidator(api, feed).compare("NFO", "123")
    assert out["verdict"] == "diverge"
    assert out["delta_pct"] > 90.0


def test_compare_when_feed_has_no_tick_then_ws_missing():
    api = FakeApi({("NSE", "1"): {"lp": "10.0"}})
    out = ShadowValidator(api, FakeFeed()).compare("NSE", "1")
    assert out["verdict"] == "ws_missing"
    assert out["rest_lp"] == 10.0


def test_compare_when_rest_returns_none_then_rest_unavailable():
    out = ShadowValidator(FakeApi(), FakeFeed({"NSE|1": {"lp": 10.0}})).compare("NSE", "1")
    assert out["verdict"] == "rest_unavailable"


def test_compare_when_rest_raises_then_rest_unavailable_not_crash():
    api = FakeApi(error=RuntimeError("broker down"))
    out = ShadowValidator(api, FakeFeed({"NSE|1": {"lp": 10.0}})).compare("NSE", "1")
    assert out["verdict"] == "rest_unavailable"


def test_compare_when_rest_lp_is_zero_then_no_division_by_zero():
    api = FakeApi({("NSE", "1"): {"lp": "0"}})
    feed = FakeFeed({"NSE|1": {"lp": 0.0}})
    out = ShadowValidator(api, feed).compare("NSE", "1")
    assert out["verdict"] == "match"


def test_compare_when_rest_lp_missing_field_then_rest_unavailable():
    api = FakeApi({("NSE", "1"): {"ts": "SOMETHING"}})
    out = ShadowValidator(api, FakeFeed()).compare("NSE", "1")
    assert out["verdict"] == "rest_unavailable"


# ── Cycle aggregation ────────────────────────────────────────────────────────

def test_run_cycle_when_mixed_instruments_then_counts_per_verdict():
    api = FakeApi({
        ("NSE", "26000"): {"lp": "100.00"},
        ("NFO", "123"): {"lp": "16.20"},
        ("NSE", "2"): None,
    })
    feed = FakeFeed({
        "NSE|26000": {"lp": 100.01},
        "NFO|123": {"lp": 99.0},
    })
    results = ShadowValidator(api, feed).run_cycle(["NSE|26000", "NFO|123", "NSE|2"])
    by_verdict = {}
    for r in results:
        by_verdict.setdefault(r["verdict"], []).append(r["key"])
    assert by_verdict == {
        "match": ["NSE|26000"],
        "diverge": ["NFO|123"],
        "rest_unavailable": ["NSE|2"],
    }
