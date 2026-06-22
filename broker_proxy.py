"""
Shoonya broker proxy — one OAuth session shared across multiple trading processes.

Reads the existing Access_token from ~/.shoonya/cred.yml, or auto-triggers
OAuth re-login if the token is stale.

Start:
    python broker_proxy.py [--port 7890] [--cred-file ~/.shoonya/cred.yml]

Both flowTrader and regimetrader set:
    BROKER_PROXY_URL=http://127.0.0.1:7890
and use BrokerClient instead of ShoonyaApiPy directly.
"""

import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import yaml
from flask import Flask, jsonify, request

# Must run from shoonya-auth root (or sys.path must include it).
sys.path.insert(0, os.path.dirname(__file__))
from api_helper import ShoonyaApiPy

log = logging.getLogger("broker_proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

_DEFAULT_CRED = os.path.expanduser("~/.shoonya/cred.yml")

# Single shared instance — all routes use this. Never instantiate a second one.
_api: ShoonyaApiPy | None = None

app = Flask(__name__)


def _init_api(cred_file: str) -> ShoonyaApiPy:
    cred_file = os.path.abspath(cred_file)
    if not os.path.exists(cred_file):
        log.error("cred file not found: %s", cred_file)
        sys.exit(1)

    # Import login helpers from login.py (same directory).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from login import _load_creds, _initialize_api_oauth

    creds = _load_creds(cred_file)
    access_token = (creds.get("Access_token") or "").strip()
    uid = (creds.get("UID") or creds.get("uid") or "").strip()
    account_id = (creds.get("Account_ID") or creds.get("actid") or uid).strip()

    api = ShoonyaApiPy()

    if access_token:
        api.inject_oauth_header(access_token, uid, account_id)
        api._NorenApi__username = uid
        api._NorenApi__accountid = account_id
        if api.validate_oauth_session():
            log.info("Proxy ready — session valid uid=%s cred=%s", uid, cred_file)
            return api
        log.warning("Access_token from %s is stale — attempting OAuth login...", cred_file)
    else:
        log.warning("No Access_token in %s — attempting OAuth login...", cred_file)

    # Auto-login path: calls _save_creds internally on success.
    _initialize_api_oauth(api, creds, log, cred_path=cred_file)
    log.info("Proxy ready — session valid after re-auth uid=%s cred=%s", uid, cred_file)
    return api


@app.route("/health", methods=["GET"])
def health():
    ok = _api is not None and _api.validate_oauth_session()
    return jsonify({"ok": ok})


@app.route("/call", methods=["POST"])
def call_method():
    data = request.get_json(force=True, silent=True) or {}
    method_name = data.get("method")
    args = data.get("args", [])
    kwargs = data.get("kwargs", {})

    if not method_name:
        return jsonify({"error": "missing 'method'"}), 400

    method = getattr(_api, method_name, None)
    if method is None:
        return jsonify({"error": f"unknown method: {method_name}"}), 400

    try:
        result = method(*args, **kwargs)
        # ShoonyaApiPy methods return dicts, lists, or None.
        if method_name == "place_order":
            log.info("DEBUG place_order kwargs=%s → result=%s", kwargs, result)
            # Also write to file — werkzeug flood scrolls stdout
            import json as _json, datetime as _dt
            _order_log = os.path.expanduser("~/git/trading/shoonya-auth/order_debug.log")
            with open(_order_log, "a") as _f:
                _f.write(f"{_dt.datetime.now().isoformat()} place_order kwargs={_json.dumps(kwargs)} result={_json.dumps(result)}\n")
        if result is None:
            return jsonify(None), 200
        return jsonify(result), 200
    except Exception as exc:
        log.error("Proxy call %s failed: %s", method_name, exc, exc_info=True)
        return jsonify({"error": str(exc)}), 502


_IST = timezone(timedelta(hours=5, minutes=30))
# Proxy shuts down at 15:40 IST — traders exit by ~15:35, this gives them a clean buffer.
_PROXY_SHUTDOWN_TIME = (15, 40)


def _market_close_watchdog() -> None:
    """Background thread: sleep until 15:40 IST, then exit the proxy."""
    now = datetime.now(_IST)
    h, m = _PROXY_SHUTDOWN_TIME
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now.weekday() >= 5:
        return  # weekend — don't auto-exit
    if now >= target:
        log.info("Started after market close (%02d:%02d IST) — shutting down", h, m)
        os._exit(0)
    delay = (target - now).total_seconds()
    log.info("Market-close watchdog armed — will shut down at %02d:%02d IST (%.0fs)", h, m, delay)
    time.sleep(delay)
    log.info("Market closed — proxy shutting down")
    os._exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shoonya OAuth broker proxy")
    parser.add_argument("--port", type=int, default=7890)
    parser.add_argument(
        "--cred-file",
        default=os.environ.get("SHOONYA_CRED_FILE", _DEFAULT_CRED),
        help="Path to cred.yml containing a valid Access_token",
    )
    args = parser.parse_args()

    _api = _init_api(args.cred_file)

    t = threading.Thread(target=_market_close_watchdog, daemon=True, name="market-close-watchdog")
    t.start()

    # threaded=True: Flask handles concurrent requests in separate threads.
    # The ShoonyaApiPy rate limiter uses threading.Lock internally — thread-safe.
    # Never use debug=True here (spawns a second process, second ShoonyaApiPy instance).
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
