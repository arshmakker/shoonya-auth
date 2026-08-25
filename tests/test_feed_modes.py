"""tests/test_feed_modes.py — feed mode semantics (TDD/BDD).

Mode string from SHOONYA_FEED_MODE decides two independent switches:
cache-serving (does /call answer from the tick cache?) and the shadow
validator (does the referee thread run?). Unknown/garbage modes must fail
safe to 'rest' semantics.
"""

import pytest

from ws_feed import cache_serving_for, normalize_mode, validator_runs_for


# ── Normalization ────────────────────────────────────────────────────────────

def test_normalize_when_valid_modes_then_passthrough():
    assert normalize_mode("shadow") == "shadow"
    assert normalize_mode("hybrid") == "hybrid"
    assert normalize_mode("rest") == "rest"


def test_normalize_when_case_or_whitespace_then_cleaned():
    assert normalize_mode("  Hybrid ") == "hybrid"
    assert normalize_mode("SHADOW") == "shadow"


def test_normalize_when_unknown_then_failsafe_rest():
    assert normalize_mode("turbo") == "rest"
    assert normalize_mode("") == "rest"


# ── Mode semantics ───────────────────────────────────────────────────────────

def test_cache_serving_when_hybrid_only_then_true():
    assert cache_serving_for("hybrid") is True
    assert cache_serving_for("shadow") is False
    assert cache_serving_for("rest") is False


def test_validator_when_feed_on_then_runs_in_both_shadow_and_hybrid():
    """The referee must keep blowing the whistle AFTER the flip — continuous
    WS-vs-REST evidence is what makes hybrid trustworthy."""
    assert validator_runs_for("shadow") is True
    assert validator_runs_for("hybrid") is True
    assert validator_runs_for("rest") is False
