"""
BrokerClient — drop-in replacement for ShoonyaApiPy that forwards all calls
to broker_proxy.py over HTTP.

Usage:
    from broker_client import BrokerClient
    api = BrokerClient("http://127.0.0.1:7890")
    # Then use api exactly like ShoonyaApiPy:
    api.get_quotes_safe("NSE", "26000")
    api.get_option_chain("NFO", "NIFTY", "24500", count=20)
    api.place_order(...)
"""

import logging

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds per proxy call


class BrokerClient:
    """
    Forwards every method call to broker_proxy.py via POST /call.
    validate_oauth_session() is mapped to GET /health (lightweight check).
    """

    def __init__(self, base_url: str = "http://127.0.0.1:7890"):
        self._base = base_url.rstrip("/")

    def validate_oauth_session(self) -> bool:
        try:
            r = requests.get(f"{self._base}/health", timeout=5)
            return r.ok and bool(r.json().get("ok"))
        except Exception as exc:
            log.warning("BrokerClient health check failed: %s", exc)
            return False

    def get_ws_order(self, order_no):
        """Latest WS order-update cache entry for one order (broker_proxy
        GET /order/<no>), fed by Noren 'om' frames — status AND fillshares
        together, without a REST round-trip. Not a ShoonyaApiPy method, so it
        bypasses __getattr__'s POST /call forwarding and hits the proxy's
        dedicated route directly.

        Returns None on a 404 ("no update seen for this order yet") or any
        transport error — both mean "don't know", never "order doesn't
        exist" or "order is REJECTED". Callers must treat None as a reason to
        fall back to REST, not as a terminal answer.
        """
        try:
            r = requests.get(f"{self._base}/order/{order_no}", timeout=5)
            if r.status_code == 404:
                return None
            if not r.ok:
                log.warning(
                    "BrokerClient.get_ws_order proxy error %d: %s",
                    r.status_code,
                    r.text[:300],
                )
                return None
            return r.json()
        except Exception as exc:
            log.warning("BrokerClient.get_ws_order failed: %s", exc)
            return None

    def ws_subscribe(self, instruments) -> bool:
        """Add instruments to the proxy's WebSocket feed (POST /subscribe).

        `instruments` are "EXCHANGE|TOKEN" strings. Subscribing is idempotent,
        so re-sending a live instrument is harmless.

        Why a caller would want this: the proxy's REST fallback inherits a
        Shoonya defect where get_quotes returns the UNDERLYING SPOT in the
        `lp` field for an option token (see the FixQ1 note in regimetrader's
        market_data.py). The WS feed does not have that defect — on 2026-09-02
        two WS-subscribed IC legs logged 7,434 clean ticks and zero bad values,
        while the two legs that fell through to REST took 150 corruptions in
        the same session. So anything holding a position wants its legs on the
        WS feed, not merely whatever the boot-time chain subscribe happened to
        cover.

        Not a ShoonyaApiPy method, so it bypasses __getattr__'s POST /call
        forwarding and hits the dedicated route directly. Returns True on
        success, False on any failure — subscribing is an optimisation, never
        a precondition, so callers must not treat False as fatal.
        """
        instruments = [i for i in (instruments or []) if isinstance(i, str) and "|" in i]
        if not instruments:
            return False
        try:
            r = requests.post(
                f"{self._base}/subscribe",
                json={"instruments": instruments},
                timeout=5,
            )
            if not r.ok:
                # 409 = SHOONYA_FEED_MODE=rest, i.e. no feed to subscribe to.
                # Expected in that configuration, not an error worth shouting about.
                log.warning(
                    "BrokerClient.ws_subscribe proxy error %d: %s",
                    r.status_code,
                    r.text[:300],
                )
                return False
            return bool(r.json().get("ok"))
        except Exception as exc:
            log.warning("BrokerClient.ws_subscribe failed: %s", exc)
            return False

    def __getattr__(self, name: str):
        def _forward(*args, **kwargs):
            payload = {"method": name, "args": list(args), "kwargs": kwargs}
            try:
                r = requests.post(
                    f"{self._base}/call",
                    json=payload,
                    timeout=_TIMEOUT,
                )
                if not r.ok:
                    log.error(
                        "BrokerClient.%s proxy error %d: %s",
                        name,
                        r.status_code,
                        r.text[:300],
                    )
                    return None
                return r.json()
            except requests.exceptions.Timeout as exc:
                log.warning("BrokerClient.%s timed out: %s", name, exc)
                return None
            except Exception as exc:
                log.error("BrokerClient.%s failed: %s", name, exc)
                return None

        return _forward
