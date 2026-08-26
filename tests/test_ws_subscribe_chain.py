"""tests/test_ws_subscribe_chain.py — wide-net chain subscription (TDD/BDD).

At boot, hybrid mode needs instruments subscribed BEFORE strategies ask for
quotes. This pins the pure logic: weekly-expiry symbol generation and the
strike window around live spot.
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from ws_subscribe_chain import (
    next_weekly_expiry,
    nifty_chain_symbols,
    position_leg_symbols,
    weekly_expiries_to_subscribe,
)


# ── Expiry ───────────────────────────────────────────────────────────────────

def test_expiry_when_today_is_monday_then_tomorrow_tuesday():
    assert next_weekly_expiry(date(2026, 8, 24)) == date(2026, 8, 25)


def test_expiry_when_today_is_tuesday_then_same_day():
    """Expiry-day morning boots must target the expiring contract."""
    assert next_weekly_expiry(date(2026, 9, 8)) == date(2026, 9, 8)


def test_expiry_when_wednesday_then_next_week_tuesday():
    assert next_weekly_expiry(date(2026, 8, 26)) == date(2026, 9, 1)


def test_expiry_matches_live_contract_convention():
    """Live legs trade as NIFTY08SEP26 — Tuesday Aug 25 2026 rolls to Sep 8."""
    assert next_weekly_expiry(date(2026, 8, 25)) == date(2026, 8, 25)
    assert weekly_expiries_to_subscribe(date(2026, 8, 25)) == (
        date(2026, 8, 25),
        date(2026, 9, 1),
        date(2026, 9, 8),
    )


def test_weekly_expiries_when_midweek_then_three_rolling_weeks():
    assert weekly_expiries_to_subscribe(date(2026, 8, 26)) == (
        date(2026, 9, 1),
        date(2026, 9, 8),
        date(2026, 9, 15),
    )


# ── Symbol generation ────────────────────────────────────────────────────────

def test_chain_when_spot_24174_then_aligned_strikes_both_sides():
    syms = nifty_chain_symbols(date(2026, 9, 8), spot=24174.0, width=100, step=50)
    # Window 24074..24274 → aligned 24100..24250 step 50 = 4 strikes × 2 sides
    assert "NIFTY08SEP26C24150" in syms
    assert "NIFTY08SEP26P24150" in syms
    assert "NIFTY08SEP26C24250" in syms
    assert "NIFTY08SEP26P24100" in syms
    assert len(syms) == 8


def test_chain_when_width_reaches_spot_then_includes_atm():
    syms = nifty_chain_symbols(date(2026, 9, 8), spot=24150.0, width=100, step=50)
    assert "NIFTY08SEP26C24150" in syms
    assert "NIFTY08SEP26P24150" in syms


def test_chain_symbol_format_matches_broker_convention():
    """Must produce exactly the shape searchscrip resolves: NIFTY08SEP26C24700."""
    syms = nifty_chain_symbols(date(2026, 9, 3), spot=24000.0, width=0, step=50)
    assert syms == ["NIFTY03SEP26C24000", "NIFTY03SEP26P24000"]


# ── Open-position leg coverage (2026-08-26) ─────────────────────────────────
# Root cause of that day's stale-LTP warnings: a position's own wing legs can
# drift outside the spot-window chain, silently losing WS coverage for the
# whole session. position_leg_symbols() must recover them regardless of
# where the strategy nests them in the persisted state.

def _write_positions(tmp_path, payload):
    path = os.path.join(tmp_path, "open_positions.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def test_position_legs_when_active_position_then_four_bare_symbols(tmp_path):
    path = _write_positions(tmp_path, {
        "strategies": {
            "NIFTY": {
                "position": {
                    "sc_sym": "NFO|NIFTY08SEP26C24850",
                    "sp_sym": "NFO|NIFTY08SEP26P23700",
                    "lc_sym": "NFO|NIFTY08SEP26C25200",
                    "lp_sym": "NFO|NIFTY08SEP26P23350",
                }
            }
        }
    })
    assert sorted(position_leg_symbols(path)) == sorted([
        "NIFTY08SEP26C24850",
        "NIFTY08SEP26P23700",
        "NIFTY08SEP26C25200",
        "NIFTY08SEP26P23350",
    ])


def test_position_legs_when_flat_then_empty(tmp_path):
    path = _write_positions(tmp_path, {
        "session_status": "flat",
        "strategies": {"NIFTY": {"last_exit_reason": "PROFIT_HARVEST"}},
        "tracker_positions": {},
    })
    assert position_leg_symbols(path) == []


def test_position_legs_when_no_file_then_empty(tmp_path):
    assert position_leg_symbols(os.path.join(tmp_path, "missing.json")) == []


def test_position_legs_when_corrupt_json_then_empty(tmp_path):
    path = os.path.join(tmp_path, "open_positions.json")
    with open(path, "w") as f:
        f.write("{not json")
    assert position_leg_symbols(path) == []


def test_position_legs_nesting_depth_agnostic(tmp_path):
    """Leg fields live wherever the owning strategy's save_state() puts them —
    must not assume a fixed 'strategies/<name>/position' schema."""
    path = _write_positions(tmp_path, {
        "tracker_positions": {
            "some_id": {
                "legs": {"deep": {"sc_sym": "NFO|NIFTY08SEP26C24850"}},
            }
        }
    })
    assert position_leg_symbols(path) == ["NIFTY08SEP26C24850"]
