"""tools/proxy_client.py — shared thin HTTP client for broker_proxy's
/call and /subscribe endpoints, used by the ws_subscribe* CLI tools."""

import json
import urllib.error
import urllib.parse
import urllib.request

PROXY = "http://127.0.0.1:7890"


def post(path, payload, timeout=15):
    req = urllib.request.Request(
        PROXY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def get(path, timeout=15):
    """GET a proxy endpoint (e.g. /tick/NFO|12345). Returns None on 404/error —
    a missing tick is an ordinary condition, not a failure."""
    try:
        req = urllib.request.Request(PROXY + urllib.parse.quote(path, safe="/|"))
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError):
        return None


def resolve(symbol):
    """Resolve a bare NFO trading symbol to an 'NFO|<token>' spec via searchscrip."""
    res = post("/call", {"method": "searchscrip", "args": ["NFO", symbol]}) or {}
    vals = res.get("values") or []
    return f"NFO|{vals[0]['token']}" if vals else None
