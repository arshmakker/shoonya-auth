"""tests/test_banknifty_ws_subscribe.py — BankNifty WS subscription discovery."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from banknifty_ws_subscribe import discover_banknifty_symbols


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def test_discover_finds_future_and_all_ce_pe_files(tmp_path):
    md = tmp_path / "market_data_20260828"
    _touch(md / "raw_data" / "futures" / "BANKNIFTY29SEP26F_20260828.csv")
    _touch(md / "raw_data" / "futures" / "NIFTY29SEP26F_20260828.csv")  # must be excluded
    _touch(md / "raw_data" / "options" / "BANKNIFTY" / "ce" / "BANKNIFTY29SEP26C57800_20260828.csv")
    _touch(md / "raw_data" / "options" / "BANKNIFTY" / "pe" / "BANKNIFTY29SEP26P57800_20260828.csv")
    _touch(md / "raw_data" / "options" / "BANKNIFTY" / "ce" / "BANKNIFTY27OCT26C57800_20260828.csv")

    symbols = discover_banknifty_symbols(md)

    assert "BANKNIFTY29SEP26F" in symbols
    assert "BANKNIFTY29SEP26C57800" in symbols
    assert "BANKNIFTY29SEP26P57800" in symbols
    assert "BANKNIFTY27OCT26C57800" in symbols
    assert "NIFTY29SEP26F" not in symbols
    assert len(symbols) == 4


def test_discover_returns_empty_when_no_files_yet(tmp_path):
    md = tmp_path / "market_data_20260828"
    assert discover_banknifty_symbols(md) == []
