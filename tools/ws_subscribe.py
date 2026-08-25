#!/usr/bin/env python
"""Resolve trading symbols via broker_proxy searchscrip and WS-subscribe them.

Usage (on the host running broker_proxy):
    ./venv/bin/python tools/ws_subscribe.py NSE|26000 NIFTY08SEP26C24700 ...

EXCHANGE|TOKEN specs pass through untouched; bare trading symbols are
resolved on NFO via /call searchscrip. Avoids embedding '|' in shell/tmux
command lines entirely.
"""

import json
import sys
import urllib.request

PROXY = "http://127.0.0.1:7890"


def post(path, payload):
    req = urllib.request.Request(
        PROXY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def main():
    specs = []
    for arg in sys.argv[1:]:
        if "|" in arg:
            specs.append(arg)
            continue
        res = post("/call", {"method": "searchscrip", "args": ["NFO", arg]})
        vals = res.get("values") or []
        if not vals:
            print(f"{arg}: NOT RESOLVED")
            continue
        specs.append(f"NFO|{vals[0]['token']}")
        print(f"{arg} -> NFO|{vals[0]['token']}")
    if not specs:
        print("nothing to subscribe")
        return
    print("SUBSCRIBE:", post("/subscribe", {"instruments": specs}))


if __name__ == "__main__":
    main()
