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
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
import yaml
from flask import Flask, jsonify, request

# Must run from shoonya-auth root (or sys.path must include it).
sys.path.insert(0, os.path.dirname(__file__))
from api_helper import ShoonyaApiPy
from quote_bridge import CACHE_MISS, QUOTE_METHODS, serve_quote_from_cache
from shadow import run_shadow_loop
from tick_persist import IST as _IST, start as start_tick_persist
from ws_feed import (
    WSFeedManager,
    cache_serving_for,
    normalize_mode,
    parse_instruments_spec,
    validator_runs_for,
)

log = logging.getLogger("broker_proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

_DEFAULT_CRED = os.path.expanduser("~/.shoonya/cred.yml")

# Single shared instance — all routes use this. Never instantiate a second one.
_api: ShoonyaApiPy | None = None

# WebSocket feed manager (owns the SDK's single WS connection + tick cache).
# None when SHOONYA_FEED_MODE=rest.
_feed: WSFeedManager | None = None

# Cache-first serving is only safe after shadow validation; shadow mode runs
# the feed as observer while consumers keep getting REST quotes.
_cache_serving_enabled = False

# Quote reads served from the WS cache must be fresher than this, else fall
# through to REST RPC.
_QUOTE_CACHE_MAX_AGE_SEC = float(os.environ.get("SHOONYA_QUOTE_CACHE_MAX_AGE", "5").strip() or 5)

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
        # inject_oauth_header sets REST headers only — the WS handshake reads
        # __access_token, without which the broker silently drops the 'a' auth
        # message and the feed never acks.
        api.set_credentials(access_token, uid, account_id)
        if api.validate_oauth_session():
            log.info("Proxy ready — session valid uid=%s cred=%s", uid, cred_file)
            return api, access_token, uid
        log.warning("Access_token from %s is stale — attempting OAuth login...", cred_file)
    else:
        log.warning("No Access_token in %s — attempting OAuth login...", cred_file)

    # Auto-login path: calls _save_creds internally on success.
    _initialize_api_oauth(api, creds, log, cred_path=cred_file)
    api.set_credentials(str(creds.get("Access_token") or "").strip(), uid, account_id)
    log.info("Proxy ready — session valid after re-auth uid=%s cred=%s", uid, cred_file)
    return api, str(creds.get("Access_token") or "").strip(), uid


def _raw_position_book(api: ShoonyaApiPy) -> dict:
    """Re-issue PositionBook directly, bypassing NorenApi.get_positions()'s
    collapsing of any non-list response to None. Needed to tell a genuinely
    flat account ({"stat":"Not_Ok","emsg":"...no data..."}) apart from a
    real broker/session error, which the library discards.
    """
    config = api._NorenApi__service_config
    url = f"{config['host']}{config['routes']['positions']}"
    values = {
        "uid": api._NorenApi__username,
        "actid": api._NorenApi__accountid,
    }
    payload = "jData=" + json.dumps(values)
    res = requests.post(url, data=payload, headers=api._NorenApi__OAuthHeaders, timeout=15)
    res.raise_for_status()
    return json.loads(res.text)


@app.route("/health", methods=["GET"])
def health():
    ok = _api is not None and _api.validate_oauth_session()
    return jsonify({"ok": ok})


@app.route("/feed/status", methods=["GET"])
def feed_status():
    if _feed is None:
        return jsonify({"enabled": False})
    status = _feed.status()
    status["enabled"] = True
    return jsonify(status)


@app.route("/tick/<key>", methods=["GET"])
def tick(key):
    if _feed is None:
        return jsonify({"error": "feed disabled (SHOONYA_FEED_MODE=rest)"}), 409
    exchange, _, token = key.partition("|")
    if not exchange or not token:
        return jsonify({"error": "key must be EXCHANGE|TOKEN"}), 400
    quote = _feed.get_quote(exchange, token, max_age_sec=float("inf"))
    if quote is None:
        return jsonify({"error": "no tick cached"}), 404
    return jsonify(quote)


@app.route("/order/<order_no>", methods=["GET"])
def order(order_no):
    """Latest WS order update for one order number.

    Exists so a consumer can learn an order's status AND fill quantity without
    polling REST. single_order_history lags (2026-07-08) and neither it nor
    get_order_book distinguishes a resting order that is partially filled from
    one that is untouched — the gap that halted the 2026-09-01 session. The
    'om' frame carries fillshares directly.

    404 means "no update seen for this order", which is NOT the same as
    "order does not exist": the socket may have connected after the order was
    placed, or dropped and reconnected. Callers must treat it as "don't know"
    and fall back to REST, never as a terminal answer.
    """
    if _feed is None:
        return jsonify({"error": "feed disabled (SHOONYA_FEED_MODE=rest)"}), 409
    record = _feed.get_order(order_no)
    if record is None:
        return jsonify({"error": "no order update cached"}), 404
    return jsonify(record)


@app.route("/orders", methods=["GET"])
def orders():
    """Every order update cached this session — operator/debug view."""
    if _feed is None:
        return jsonify({"error": "feed disabled (SHOONYA_FEED_MODE=rest)"}), 409
    return jsonify(_feed.all_orders())


@app.route("/subscribe", methods=["POST"])
def subscribe_instruments():
    data = request.get_json(force=True, silent=True) or {}
    instruments = data.get("instruments")
    action = data.get("action", "subscribe")
    if not isinstance(instruments, list) or not instruments or \
            not all(isinstance(i, str) and "|" in i for i in instruments):
        return jsonify({"error": "'instruments' must be a non-empty list of 'EXCHANGE|TOKEN' strings"}), 400
    if _feed is None:
        return jsonify({"error": "feed disabled (SHOONYA_FEED_MODE=rest)"}), 409
    if action == "unsubscribe":
        _feed.unsubscribe(instruments)
    else:
        _feed.subscribe(instruments)
    return jsonify({"ok": True, "subscriptions": _feed.status()["subscriptions"]})


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

    # Cache-first for quote reads: fresh WS tick beats a REST round-trip.
    if _cache_serving_enabled and _feed is not None and method_name in QUOTE_METHODS:
        cached = serve_quote_from_cache(
            _feed, method_name, args, kwargs, max_age_sec=_QUOTE_CACHE_MAX_AGE_SEC
        )
        if cached is not CACHE_MISS:
            return jsonify(cached), 200

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
        if method_name == "get_positions" and result is None:
            # A flat account and a real broker error both collapse to None
            # here (see _raw_position_book docstring) — recover the raw
            # stat/emsg to tell them apart before the caller has to halt.
            try:
                raw = _raw_position_book(_api)
            except Exception as exc:
                log.error("Proxy get_positions raw re-check failed: %s", exc, exc_info=True)
                return jsonify({"error": f"positions re-check failed: {exc}"}), 502
            if isinstance(raw, dict) and "no data" in str(raw.get("emsg", "")).lower():
                return jsonify([]), 200
            emsg = raw.get("emsg") if isinstance(raw, dict) else "malformed positions response"
            log.error("Proxy get_positions: broker reported error: %s", emsg)
            return jsonify({"error": emsg}), 502
        if result is None:
            return jsonify(None), 200
        return jsonify(result), 200
    except Exception as exc:
        log.error("Proxy call %s failed: %s", method_name, exc, exc_info=True)
        return jsonify({"error": str(exc)}), 502


# When the proxy ends its day (IST), as HH:MM in SHOONYA_SHUTDOWN_TIME.
#
# The default is 15:40 — a clean buffer past the 15:30 NSE close, which is all a
# session that only trades NSE/NFO needs. start.sh (Mac) is exactly that: it
# passes no SHOONYA_TICK_PERSIST_DIR and subscribes no MCX instruments, so after
# 15:40 nothing consumes the feed and the only thing an open proxy achieves is
# holding a live authenticated broker session for another eight hours.
#
# start_vps.sh overrides it to 23:58, because the droplet DOES capture MCX. MCX
# closes 23:30, or 23:55 on US daylight-saving days, so 23:58 clears both while
# staying on the same calendar day. That override used to be this constant's
# hardcoded value, which silently imposed the droplet's MCX tail on every local
# run too.
#
# Note tick_persist._SESSION_END stays hardcoded and is deliberately NOT coupled
# to this. Its per-exchange windows bound how long a frozen quote may keep
# emitting heartbeat rows; a window wider than the shutdown time simply never
# gets reached. Narrowing them to match would be a behaviour change, not a fix.
_DEFAULT_SHUTDOWN_TIME = "15:40"


def _resolve_shutdown_time(raw: str | None) -> tuple[int, int]:
    """Parse SHOONYA_SHUTDOWN_TIME ("HH:MM", IST) into (hour, minute).

    A malformed value is fatal rather than falling back to the default. The
    default is 15:40, so a typo in start_vps.sh would otherwise end the
    droplet's day eight hours early and take the entire MCX evening with it —
    unrecoverable data, and invisible until someone reads the tick files. A
    refusal to boot is noisy and fixable in a minute; the quiet 15:40 is not.
    """
    raw = (raw or "").strip() or _DEFAULT_SHUTDOWN_TIME
    try:
        hh, mm = raw.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(raw)
    except ValueError:
        raise SystemExit(
            f"SHOONYA_SHUTDOWN_TIME must be HH:MM on a 24-hour clock (IST); got {raw!r}"
        )
    return hour, minute


def _market_close_watchdog(shutdown_time: tuple[int, int]) -> None:
    """Background thread: sleep until the shutdown time (IST), then exit."""
    now = datetime.now(_IST)
    h, m = shutdown_time
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now.weekday() >= 5:
        return  # weekend — don't auto-exit
    if now >= target:
        log.info("Started after session close (%02d:%02d IST) — shutting down", h, m)
        os._exit(0)
    delay = (target - now).total_seconds()
    log.info("Session-close watchdog armed — will shut down at %02d:%02d IST (%.0fs)", h, m, delay)
    time.sleep(delay)
    log.info("Session closed — proxy shutting down")
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

    # Resolved BEFORE _init_api, so a malformed SHOONYA_SHUTDOWN_TIME fails in
    # milliseconds rather than after a ~30s Selenium OAuth round-trip.
    #
    # The log line reports the value AND its source. The 2026-08-21 paper/live
    # incident turned on exactly this: a setting the operator believed one thing
    # about while the process ran another, with nothing in the log to contradict
    # them. "15:40 (default)" vs "23:58 (SHOONYA_SHUTDOWN_TIME)" is unambiguous.
    _shutdown_raw = os.environ.get("SHOONYA_SHUTDOWN_TIME")
    shutdown_time = _resolve_shutdown_time(_shutdown_raw)
    log.info(
        "Session-close time: %02d:%02d IST (%s)",
        *shutdown_time,
        "SHOONYA_SHUTDOWN_TIME" if (_shutdown_raw or "").strip() else "default",
    )

    _api, ws_access_token, ws_uid = _init_api(args.cred_file)

    feed_mode = normalize_mode(os.environ.get("SHOONYA_FEED_MODE", "hybrid"))
    if feed_mode == "rest":
        log.info("SHOONYA_FEED_MODE=rest — WebSocket feed disabled")
    else:
        _feed = WSFeedManager(access_token=ws_access_token, uid=ws_uid)
        _feed.start()
        auto_subscribe = parse_instruments_spec(os.environ.get("SHOONYA_WS_SUBSCRIBE", ""))
        if auto_subscribe:
            _feed.subscribe(auto_subscribe)
            log.info("Auto-subscribed %d instruments from SHOONYA_WS_SUBSCRIBE", len(auto_subscribe))
        # Persist whatever is subscribed. In-process because this is where the
        # tick store already lives: a separate collector would cost another
        # Python interpreter (~30-80MB of a 1GB box) plus an HTTP round-trip per
        # instrument, to read memory we already hold. Snapshot thread, not a
        # request hook — a slow disk stalls only the writer.
        start_tick_persist(
            _feed,
            os.environ.get("SHOONYA_TICK_PERSIST_DIR", ""),
            float(os.environ.get("SHOONYA_TICK_PERSIST_SEC", "5").strip() or 5),
        )

        _cache_serving_enabled = cache_serving_for(feed_mode)
        if validator_runs_for(feed_mode):
            interval = float(os.environ.get("SHOONYA_SHADOW_INTERVAL", "30").strip() or 30)
            threading.Thread(
                target=run_shadow_loop,
                args=(_api, _feed, interval),
                daemon=True,
                name="ws-shadow-validator",
            ).start()
            log.info(
                "SHOONYA_FEED_MODE=%s — cache-serving=%s, validating subscribed instruments every %ss",
                feed_mode,
                _cache_serving_enabled,
                interval,
            )

    t = threading.Thread(
        target=_market_close_watchdog,
        args=(shutdown_time,),
        daemon=True,
        name="market-close-watchdog",
    )
    t.start()

    # threaded=True: Flask handles concurrent requests in separate threads.
    # The ShoonyaApiPy rate limiter uses threading.Lock internally — thread-safe.
    # Never use debug=True here (spawns a second process, second ShoonyaApiPy instance).
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
