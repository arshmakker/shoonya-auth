"""tests/test_quote_crosswire_guard.py — reject cross-wired getquotes responses.

Shoonya's getquotes endpoint occasionally returns a DIFFERENT instrument's
payload than the one requested under concurrency (observed live, ~5-17%,
scaling with concurrent request volume). _quote_request must not hand a
caller a quote it did not ask for — trading on another instrument's price is
worse than a transient error, since nothing else in the stack can detect it.

Verified live against the droplet (2026-09-11) that "token"/"exch" are
present in real getquotes responses for every exchange this system trades:
NFO options, NSE equities, NSE index, and MCX.
"""

import json

import pytest
from NorenRestApiPy.NorenApi import NorenApi

from api_helper import ShoonyaApiPy


def _make_sdk(monkeypatch):
    # The SDK constructor mutates shared class-level configuration; restore
    # it after each test so other tests see the original service config.
    monkeypatch.setattr(NorenApi, "_NorenApi__service_config", dict(NorenApi._NorenApi__service_config))
    api = ShoonyaApiPy()
    api.injectOAuthHeader("TEST-TOKEN", "TEST-USER", "TEST-USER")
    return api


def _ok_response(**fields):
    body = {"stat": "Ok", "lp": "100.00", "token": "26000", "exch": "NSE", **fields}
    return json.dumps(body)


class _FakeHTTPResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    @property
    def ok(self):
        return 200 <= self.status_code < 300


def test_quote_request_when_response_matches_request_then_returned(monkeypatch):
    api = _make_sdk(monkeypatch)
    monkeypatch.setattr(
        "api_helper.requests.post",
        lambda *a, **k: _FakeHTTPResponse(_ok_response(token="26000", exch="NSE")),
    )
    data, err = api._quote_request("NSE", "26000")
    assert err == ""
    assert data["token"] == "26000"


def test_quote_request_when_response_is_a_different_token_then_rejected(monkeypatch):
    """The broker crossed wires: we asked for token 26000, got 47265 back."""
    api = _make_sdk(monkeypatch)
    monkeypatch.setattr(
        "api_helper.requests.post",
        lambda *a, **k: _FakeHTTPResponse(_ok_response(token="47265", exch="NFO")),
    )
    data, err = api._quote_request("NSE", "26000")
    assert data is None
    assert "cross" in err.lower() or "mismatch" in err.lower()


def test_quote_request_when_response_is_a_different_exchange_then_rejected(monkeypatch):
    """Same token number, different exchange — still a wrong instrument."""
    api = _make_sdk(monkeypatch)
    monkeypatch.setattr(
        "api_helper.requests.post",
        lambda *a, **k: _FakeHTTPResponse(_ok_response(token="26000", exch="MCX")),
    )
    data, err = api._quote_request("NSE", "26000")
    assert data is None


def test_quote_request_when_exch_case_differs_then_accepted(monkeypatch):
    """Exchange comparison must be case-insensitive — not itself a crosswire."""
    api = _make_sdk(monkeypatch)
    monkeypatch.setattr(
        "api_helper.requests.post",
        lambda *a, **k: _FakeHTTPResponse(_ok_response(token="26000", exch="nse")),
    )
    data, err = api._quote_request("NSE", "26000")
    assert err == ""
    assert data is not None


def test_get_quotes_safe_when_first_attempt_crosswired_then_retry_recovers(monkeypatch):
    """get_quotes_safe's existing retry must absorb a transient crosswire —
    the guard should cost a retry, not surface as a permanent failure."""
    api = _make_sdk(monkeypatch)
    calls = {"n": 0}

    def flaky_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeHTTPResponse(_ok_response(token="99999", exch="BSE"))
        return _FakeHTTPResponse(_ok_response(token="26000", exch="NSE"))

    monkeypatch.setattr("api_helper.requests.post", flaky_post)
    result = api.get_quotes_safe("NSE", "26000", retries=1, backoff_sec=0)
    assert result is not None
    assert result["token"] == "26000"
    assert calls["n"] == 2


def test_get_quotes_safe_when_always_crosswired_then_none_not_wrong_data(monkeypatch):
    """If every attempt is crosswired, the caller must get None — never a
    quote for an instrument it did not ask for."""
    api = _make_sdk(monkeypatch)
    monkeypatch.setattr(
        "api_helper.requests.post",
        lambda *a, **k: _FakeHTTPResponse(_ok_response(token="99999", exch="BSE")),
    )
    result = api.get_quotes_safe("NSE", "26000", retries=1, backoff_sec=0)
    assert result is None


# ── jKey-retry branch must apply the same guard ──────────────────────────────

def test_quote_request_when_jkey_retry_response_crosswired_then_rejected(monkeypatch):
    """OAuth-header auth gets a 401, falls back to jKey retry — that retry
    response must be checked too; it was NOT before this fix."""
    api = _make_sdk(monkeypatch)
    api._NorenApi__susertoken = "TEST-SUSERTOKEN"

    calls = {"n": 0}

    def post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            assert headers is not None  # the OAuth-header attempt
            return _FakeHTTPResponse('{"emsg": "Invalid Session Key"}', status_code=401)
        return _FakeHTTPResponse(_ok_response(token="47265", exch="NFO"))

    monkeypatch.setattr("api_helper.requests.post", post)
    data, err = api._quote_request("NSE", "26000")
    assert data is None
    assert calls["n"] == 2
