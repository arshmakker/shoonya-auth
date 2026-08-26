#!/usr/bin/env python
"""Resolve trading symbols via broker_proxy searchscrip and WS-subscribe them.

Usage (on the host running broker_proxy):
    ./venv/bin/python tools/ws_subscribe.py NSE|26000 NIFTY08SEP26C24700 ...

EXCHANGE|TOKEN specs pass through untouched; bare trading symbols are
resolved on NFO via /call searchscrip. Avoids embedding '|' in shell/tmux
command lines entirely.
"""

import sys

from proxy_client import post, resolve


def main():
    specs = []
    for arg in sys.argv[1:]:
        if "|" in arg:
            specs.append(arg)
            continue
        try:
            spec = resolve(arg)
        except Exception as exc:
            print(f"{arg}: resolve failed ({exc})")
            continue
        if not spec:
            print(f"{arg}: NOT RESOLVED")
            continue
        specs.append(spec)
        print(f"{arg} -> {spec}")
    if not specs:
        print("nothing to subscribe")
        return
    print("SUBSCRIBE:", post("/subscribe", {"instruments": specs}))


if __name__ == "__main__":
    main()
