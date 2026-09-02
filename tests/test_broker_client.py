"""Tests for BrokerClient.get_ws_order — the WS order-cache accessor used to
short-circuit REST polling in regimetrader's live_order_manager. This is a
GET to broker_proxy's dedicated /order/<no> route, not a ShoonyaApiPy method
forwarded through __getattr__'s POST /call, so it needs its own coverage.
"""

from unittest.mock import MagicMock, patch

from broker_client import BrokerClient


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


class TestGetWsOrder:
    def test_returns_order_record_on_success(self):
        client = BrokerClient("http://127.0.0.1:7890")
        record = {"norenordno": "X1", "status": "COMPLETE", "fillshares": 650}
        with patch("requests.get", return_value=_mock_response(200, record)) as mock_get:
            result = client.get_ws_order("X1")
        assert result == record
        mock_get.assert_called_once_with("http://127.0.0.1:7890/order/X1", timeout=5)

    def test_404_returns_none_not_terminal(self):
        """A 404 means 'no update seen yet', never 'order doesn't exist' or
        REJECTED — the caller must fall back to REST, so this must return
        None rather than an empty dict or an error record a caller might
        misread as terminal."""
        client = BrokerClient()
        with patch("requests.get", return_value=_mock_response(404)):
            result = client.get_ws_order("X2")
        assert result is None

    def test_non_ok_status_returns_none(self):
        client = BrokerClient()
        with patch("requests.get", return_value=_mock_response(500, text="boom")):
            result = client.get_ws_order("X3")
        assert result is None

    def test_transport_error_returns_none(self):
        client = BrokerClient()
        with patch("requests.get", side_effect=Exception("connection refused")):
            result = client.get_ws_order("X4")
        assert result is None

    def test_does_not_go_through_call_forwarding(self):
        """get_ws_order must hit /order/<no> directly, never POST /call —
        proving it bypasses __getattr__'s ShoonyaApiPy-method forwarding."""
        client = BrokerClient()
        with patch("requests.get", return_value=_mock_response(200, {})) as mock_get, \
                patch("requests.post") as mock_post:
            client.get_ws_order("X5")
        mock_get.assert_called_once()
        mock_post.assert_not_called()
