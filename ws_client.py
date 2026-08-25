"""ws_client.py — minimal WebSocket transport for the Shoonya feed.

Own connect/reconnect/handshake directly over websocket-client, bypassing
NorenApi.start_websocket (which silently never received its auth ack inside
the proxy). Handshake shape and NorenWS endpoint are probe-verified against
the live broker with a real session token.
"""

import json
import logging
import threading
import time

import websocket

log = logging.getLogger("ws_client")

WS_URL = "wss://api.shoonya.com/NorenWS/"
RECONNECT_DELAY_SEC = 1.0


class WsClient:
    def __init__(self, access_token, uid, on_message, reconnect_delay=RECONNECT_DELAY_SEC):
        self._handshake = {
            "t": "a",
            "uid": uid,
            "actid": uid,
            "accesstoken": access_token,
            "source": "API",
        }
        self._on_message = on_message
        self._reconnect_delay = reconnect_delay
        self._ws = None
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()

    def start(self):
        self._stop_event.clear()
        threading.Thread(target=self._run_loop, daemon=True, name="ws-client").start()

    def send(self, text):
        with self._send_lock:
            if self._ws is None:
                log.warning("send skipped — socket not open")
                return False
            try:
                self._ws.send(text)
                return True
            except Exception as exc:
                log.error("send failed: %s", exc)
                return False

    def close(self):
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=3, ping_payload=json.dumps({"t": "h"}))
            except Exception as exc:
                log.error("websocket loop error: %s", exc)
            if not self._stop_event.is_set():
                log.warning("ws down — reconnecting in %ss", self._reconnect_delay)
                time.sleep(self._reconnect_delay)

    def _on_open(self, ws):
        ws.send(json.dumps(self._handshake))

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            return
        self._on_message(data)

    def _on_error(self, ws, error):
        log.error("ws error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        log.warning("ws closed (%s)", close_status_code)
