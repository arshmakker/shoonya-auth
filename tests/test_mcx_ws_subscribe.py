"""tests/test_mcx_ws_subscribe.py — MCX liquid-5 WS subscription selection."""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from mcx_ws_subscribe import ALL_UNDERLYINGS, LIQUID_5, MINIS, parse_mcx_master, select_front_two

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "MCX_sample.csv")


def test_select_front_two_picks_two_earliest_non_expired_per_underlying():
    rows = parse_mcx_master(FIXTURE)
    selected = select_front_two(rows, LIQUID_5, today=date(2026, 8, 28))
    by_symbol = {}
    for c in selected:
        by_symbol.setdefault(c["symbol"], []).append(c["trading_symbol"])

    assert by_symbol["GOLD"] == ["GOLD05SEP26", "GOLD05OCT26"]
    assert by_symbol["SILVER"] == ["SILVER31AUG26", "SILVER30SEP26"]
    assert by_symbol["CRUDEOIL"] == ["CRUDEOIL21SEP26"]  # only one non-expired row in fixture
    assert "COPPER" not in by_symbol  # both fixture rows already expired
    assert "NATURALGAS" not in by_symbol  # fixture row is OPTFUT, not FUTCOM
    assert len(selected) == 2 + 2 + 1  # GOLD, SILVER, CRUDEOIL


def test_select_front_two_skips_expired_contracts():
    rows = parse_mcx_master(FIXTURE)
    selected = select_front_two(rows, ["COPPER"], today=date(2026, 8, 28))
    assert selected == []


def test_select_front_two_ignores_non_futcom_rows():
    rows = parse_mcx_master(FIXTURE)
    selected = select_front_two(rows, ["NATURALGAS"], today=date(2026, 8, 28))
    assert selected == []


def test_all_underlyings_is_liquid_5_plus_minis_no_copper_mini():
    assert ALL_UNDERLYINGS == LIQUID_5 + MINIS
    assert "COPPERM" not in MINIS  # no mini exists for COPPER


def test_select_front_two_picks_up_mini_variants():
    rows = parse_mcx_master(FIXTURE)
    selected = select_front_two(rows, ALL_UNDERLYINGS, today=date(2026, 8, 28))
    by_symbol = {}
    for c in selected:
        by_symbol.setdefault(c["symbol"], []).append(c["trading_symbol"])

    assert by_symbol["GOLDM"] == ["GOLDM04SEP26", "GOLDM04OCT26"]
    assert by_symbol["SILVERM"] == ["SILVERM31AUG26"]
    assert "GOLDGUINEA" not in by_symbol  # no fixture rows — must not error
