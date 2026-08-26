"""tools/proxy_client.py — shared thin HTTP client for broker_proxy's
/call and /subscribe endpoints, used by the ws_subscribe* CLI tools."""

import json
import urllib.request

PROXY = "http://127.0.0.1:7890"


def post(path, payload, timeout=15):
    req = urllib.request.Request(
        PROXY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def resolve(symbol):
    """Resolve a bare NFO trading symbol to an 'NFO|<token>' spec via searchscrip."""
    res = post("/call", {"method": "searchscrip", "args": ["NFO", symbol]}) or {}
    vals = res.get("values") or []
    return f"NFO|{vals[0]['token']}" if vals else None
