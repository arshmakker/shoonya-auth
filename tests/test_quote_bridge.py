"""tests/test_quote_bridge.py — cache-first quote serving (TDD/BDD).

broker_proxy's POST /call route intercepts get_quotes / get_quotes_safe and
tries the WS tick cache BEFORE falling back to the REST RPC. This module holds
that decision logic as a pure function so it's testable without Flask.
"""

import pytest

from quote_bridge import QUOTE_METHODS, serve_quote_from_cache


class FakeFeed:
    def __init__(self, quotes=None):
        self.quotes = quotes or {}
        self.asked = []

    def get_quote(self, exchange, token, max_age_sec=None):
        self.asked.append((exchange, token, max_age_sec))
        return self.quotes.get((exchange, token))


MISS = object()  # mirror of quote_bridge.CACHE_MISS semantics via identity check


def _miss(result):
    return result is quote_bridge_miss()


def quote_bridge_miss():
    from quote_bridge import CACHE_MISS
    return CACHE_MISS


# ── Method selection ─────────────────────────────────────────────────────────

def test_quote_methods_when_checked_then_only_read_paths_intercepted():
    assert "get_quotes" in QUOTE_METHODS
    assert "get_quotes_safe" in QUOTE_METHODS
    assert "place_order" not in QUOTE_METHODS
    assert "get_positions" not in QUOTE_METHODS


# ── Cache hits ───────────────────────────────────────────────────────────────

def test_serve_when_get_quotes_cached_then_quote_returned_not_miss():
    feed = FakeFeed({("NSE", "26000"): {"lp": 24170.0}})
    out = serve_quote_from_cache(feed, "get_quotes", ["NSE", "26000"], {})
    assert out is not quote_bridge_miss()
    assert out["lp"] == 24170.0
    assert feed.asked == [("NSE", "26000", None)]


def test_serve_when_get_quotes_safe_with_kwargs_then_token_extracted():
    feed = FakeFeed({("NFO", "123"): {"lp": 7.5}})
    out = serve_quote_from_cache(feed, "get_quotes_safe", [], {"exchange": "NFO", "token": "123"})
    assert out is not quote_bridge_miss()
    assert feed.asked[0][:2] == ("NFO", "123")


# ── Cache misses → RPC fallback ─────────────────────────────────────────────

def test_serve_when_symbol_not_cached_then_miss():
    out = serve_quote_from_cache(FakeFeed(), "get_quotes", ["NSE", "404"], {})
    assert out is quote_bridge_miss()


def test_serve_when_feed_is_none_then_miss_without_crash():
    out = serve_quote_from_cache(None, "get_quotes", ["NSE", "1"], {})
    assert out is quote_bridge_miss()


def test_serve_when_method_not_a_quote_read_then_miss():
    out = serve_quote_from_cache(FakeFeed(), "place_order", [], {"symbol": "X"})
    assert out is quote_bridge_miss()


def test_serve_when_args_missing_token_then_miss_not_exception():
    out = serve_quote_from_cache(FakeFeed(), "get_quotes", [], {})
    assert out is quote_bridge_miss()


def test_serve_when_max_age_configured_then_passed_to_feed():
    feed = FakeFeed({("NSE", "1"): {"lp": 1.0}})
    serve_quote_from_cache(feed, "get_quotes", ["NSE", "1"], {}, max_age_sec=3.0)
    assert feed.asked[0][2] == 3.0
