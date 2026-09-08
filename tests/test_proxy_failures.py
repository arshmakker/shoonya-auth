"""Broker responses must survive local failures and recovery requests."""

from unittest.mock import Mock, mock_open, patch

import pytest

import broker_proxy


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
