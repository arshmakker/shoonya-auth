"""Exercise the actual transport callbacks without opening a socket."""

import json
from unittest.mock import Mock

import pytest

from ws_feed import WSFeedManager


@pytest.fixture
def connected_feed():
    feed = WSFeedManager("test-token", "test-user")
    socket = Mock()
    feed._transport._ws = socket
    feed._transport._on_ws_message(socket, json.dumps({"t": "ak", "s": "OK"}))
    assert feed.status()["connected"] is True
    socket.reset_mock()
    return feed, socket


def test_disconnect_defers_subscriptions_until_new_ack(connected_feed):
    feed, socket = connected_feed
    feed._transport._on_close(socket, 1006, "connection lost")
    assert feed.status()["connected"] is False
    feed.subscribe(["NFO|123"])
    socket.send.assert_not_called()
    feed._transport._on_open(socket)
    assert feed.status()["connected"] is False
    socket.reset_mock()
    feed._transport._on_ws_message(socket, json.dumps({"t": "ak", "s": "OK"}))
    assert feed.status()["connected"] is True
    sent = [json.loads(call.args[0]) for call in socket.send.call_args_list]
    assert sent == [{"t": "t", "k": "NFO|123"}, {"t": "o", "actid": "test-user"}]


def test_stop_clears_connected_even_without_close_callback(connected_feed):
    feed, socket = connected_feed
    feed.stop()
    assert feed.status()["connected"] is False
    socket.close.assert_called_once_with()


def test_rejected_ack_clears_previous_connection(connected_feed):
    feed, socket = connected_feed
    feed._transport._on_ws_message(socket, json.dumps({"t": "ak", "s": "Not_Ok"}))
    assert feed.status()["connected"] is False
    assert "Not_Ok" in feed.status()["last_error"]
    socket.send.assert_not_called()
