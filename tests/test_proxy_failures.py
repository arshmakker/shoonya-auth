"""Broker responses must survive local failures and recovery requests."""

import json
from unittest.mock import Mock, mock_open, patch

import pytest
import requests
from NorenRestApiPy.NorenApi import NorenApi

import broker_proxy
from broker_client import BrokerClient


@pytest.fixture
def proxy(monkeypatch):
    api = Mock()
    monkeypatch.setattr(broker_proxy, "_api", api)
    monkeypatch.setattr(broker_proxy, "_cache_serving_enabled", False)
    return broker_proxy.app.test_client(), api


@pytest.mark.parametrize("failure_at", ["open", "write", "close"])
def test_order_ack_survives_debug_log_failure(proxy, failure_at):
    client, api = proxy
    ack = {"stat": "Ok", "norenordno": "TEST-ORDER"}
    api.place_order.return_value = ack
    file_mock = mock_open()
    error = OSError("simulated disk failure")
    if failure_at == "open":
        file_mock.side_effect = error
    elif failure_at == "write":
        file_mock.return_value.write.side_effect = error
    else:
        file_mock.return_value.__exit__.side_effect = error
    with patch("builtins.open", file_mock):
        response = client.post("/call", json={"method": "place_order", "kwargs": {"quantity": 1}})
    assert response.status_code == 200
    assert response.json == ack
    api.place_order.assert_called_once_with(quantity=1)


def test_broker_order_exception_still_returns_error(proxy):
    client, api = proxy
    api.place_order.side_effect = RuntimeError("broker unavailable")
    response = client.post("/call", json={"method": "place_order"})
    assert response.status_code == 502
    assert response.json == {"error": "broker unavailable"}
    api.place_order.assert_called_once_with()


@pytest.mark.parametrize("positions", [[], [{"tsym": "TEST", "netqty": "1"}]])
def test_positions_recovery_returns_valid_list(proxy, positions):
    client, api = proxy
    api.get_positions.return_value = None
    with patch.object(broker_proxy, "_raw_position_book", return_value=positions) as retry:
        response = client.post("/call", json={"method": "get_positions"})
    assert response.status_code == 200
    assert response.json == positions
    retry.assert_called_once_with(api)


@pytest.mark.parametrize("raw, status, expected", [
    ({"stat": "Not_Ok", "emsg": "no data"}, 200, []),
    ({"stat": "Not_Ok", "emsg": "Session expired"}, 502, {"error": "Session expired"}),
    (None, 502, {"error": "malformed positions response"}),
])
def test_positions_recovery_distinguishes_flat_from_errors(proxy, raw, status, expected):
    client, api = proxy
    api.get_positions.return_value = None
    with patch.object(broker_proxy, "_raw_position_book", return_value=raw):
        response = client.post("/call", json={"method": "get_positions"})
    assert response.status_code == status
    assert response.json == expected


@pytest.mark.parametrize("orders", [[], [{"status": "OPEN"}], [{"status": "COMPLETE"}]])
def test_order_book_preserves_sdk_list_without_retry(proxy, orders):
    client, api = proxy
    api.get_order_book.return_value = orders
    with patch.object(broker_proxy, "_raw_order_book") as retry:
        response = client.post("/call", json={"method": "get_order_book"})
    assert response.status_code == 200
    assert response.json == orders
    retry.assert_not_called()


@pytest.mark.parametrize("raw, status, expected", [
    ({"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}, 200, []),
    ({"stat": "Not_Ok", "emsg": "no data"}, 200, []),
    ([], 200, []),
    ([{"status": "OPEN", "norenordno": "TEST"}], 200, [{"status": "OPEN", "norenordno": "TEST"}]),
    ({"stat": "Not_Ok", "emsg": "Session expired"}, 502, {"error": "Session expired"}),
    ({"stat": "Not_Ok", "emsg": "Service unavailable: no data"}, 502, {"error": "Service unavailable: no data"}),
    ({"stat": "Ok", "emsg": "no data"}, 502, {"error": "no data"}),
    ({}, 502, {"error": "malformed order book response"}),
    (None, 502, {"error": "malformed order book response"}),
])
def test_order_book_recovery_distinguishes_empty_from_errors(proxy, raw, status, expected):
    client, api = proxy
    api.get_order_book.return_value = None
    api._NorenApi__service_config = {"host": "https://broker.invalid", "routes": {"orderbook": "/OrderBook"}}
    api._NorenApi__username = "TEST-USER"
    api._NorenApi__OAuthHeaders = {"Authorization": "Bearer TEST-TOKEN"}
    upstream = Mock(text=json.dumps(raw))
    with patch.object(broker_proxy.requests, "post", return_value=upstream) as post:
        response = client.post("/call", json={"method": "get_order_book"})
    assert response.status_code == status
    assert response.json == expected
    post.assert_called_once_with(
        "https://broker.invalid/OrderBook",
        data='jData={"ordersource": "API", "uid": "TEST-USER"}',
        headers={"Authorization": "Bearer TEST-TOKEN"},
        timeout=15,
    )
    upstream.raise_for_status.assert_called_once_with()


@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
def test_order_book_recovery_failure_never_reports_empty(proxy, failure):
    client, api = proxy
    api.get_order_book.return_value = None
    api._NorenApi__service_config = {"host": "https://broker.invalid", "routes": {"orderbook": "/OrderBook"}}
    api._NorenApi__username = "TEST-USER"
    api._NorenApi__OAuthHeaders = {}
    upstream = Mock(text="invalid JSON")
    if failure == "http":
        # Even an empty-book body cannot override an unsuccessful HTTP status.
        upstream.text = json.dumps({"stat": "Not_Ok", "emsg": "no data"})
        upstream.raise_for_status.side_effect = requests.HTTPError("503 unavailable")
    with patch.object(broker_proxy.requests, "post", return_value=upstream) as post:
        if failure == "timeout":
            post.side_effect = requests.Timeout("timed out")
        response = client.post("/call", json={"method": "get_order_book"})
    assert response.status_code == 502
    assert response.json["error"].startswith("order book re-check failed:")


def test_empty_order_book_startup_regression_20260909(monkeypatch):
    """The droplet halted its daily reset because an empty book became None.

    Replay the exact broker response through the real SDK, proxy route and
    BrokerClient used by regimetrader. No live broker or credentials required.
    The consumer must receive [] so it can verify that no orders are pending.
    """
    # The SDK constructor mutates shared configuration; restore it after this test.
    monkeypatch.setattr(NorenApi, "_NorenApi__service_config", dict(NorenApi._NorenApi__service_config))
    sdk = NorenApi(host="https://broker.invalid", websocket="wss://broker.invalid")
    sdk.injectOAuthHeader("TEST-TOKEN", "TEST-USER", "TEST-USER")
    monkeypatch.setattr(broker_proxy, "_api", sdk)
    monkeypatch.setattr(broker_proxy, "_cache_serving_enabled", False)
    flask_client = broker_proxy.app.test_client()
    raw_empty_book = {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}

    def transport(url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        if url == "http://proxy.invalid/call":
            routed = flask_client.post("/call", json=kwargs["json"])
            response.status_code = routed.status_code
            response._content = routed.data
        elif url == "https://broker.invalid/OrderBook":
            response._content = json.dumps(raw_empty_book).encode()
        else:
            raise AssertionError(f"Unexpected request: {url}")
        return response

    with patch.object(requests, "post", side_effect=transport):
        # Pin the SDK behavior that caused the incident, rather than mocking
        # get_order_book() to return None and assuming the SDK does so.
        assert sdk.get_order_book() is None
        orders = BrokerClient("http://proxy.invalid").get_order_book()

    assert orders == [], "Confirmed empty order book must not block the daily reset as unknown"
