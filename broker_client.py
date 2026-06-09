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
