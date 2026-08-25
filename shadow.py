"""shadow.py — WS-vs-REST shadow validator.

Runs the WebSocket feed as a pure observer: every cycle compares cached WS
ticks against fresh REST quotes and classifies each instrument. Evidence from
this gate decides when cache-first serving is safe to enable.
"""

import logging
import time

log = logging.getLogger("shadow")


class ShadowValidator:
    def __init__(self, api, feed, divergence_tol_pct=1.0):
        self._api = api
        self._feed = feed
        self._tol_pct = divergence_tol_pct

    def compare(self, exchange, token):
        key = f"{exchange}|{token}"

        try:
            rest_quote = self._api.get_quotes(exchange, token)
        except Exception as exc:
            return {"key": key, "verdict": "rest_unavailable", "error": str(exc)}
        rest_lp = None
        if isinstance(rest_quote, dict):
            try:
                rest_lp = float(rest_quote["lp"])
            except (KeyError, TypeError, ValueError):
                rest_lp = None
        if rest_lp is None:
            return {"key": key, "verdict": "rest_unavailable"}

        ws_quote = self._feed.get_quote(exchange, token)
        if not isinstance(ws_quote, dict) or "lp" not in ws_quote:
            return {"key": key, "verdict": "ws_missing", "rest_lp": rest_lp}
        ws_lp = float(ws_quote["lp"])

        if rest_lp == 0:
            delta_pct = 0.0 if ws_lp == 0 else 100.0
        else:
            delta_pct = abs(ws_lp - rest_lp) / rest_lp * 100.0
        verdict = "match" if delta_pct <= self._tol_pct else "diverge"
        return {
            "key": key,
            "verdict": verdict,
            "ws_lp": ws_lp,
            "rest_lp": rest_lp,
            "delta_pct": round(delta_pct, 3),
        }

    def run_cycle(self, instruments):
        results = []
        for spec in instruments:
            exchange, _, token = str(spec).partition("|")
            results.append(self.compare(exchange, token))

        counts = {}
        for r in results:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        divergent = [r for r in results if r["verdict"] == "diverge"]
        log.info(
            "SHADOW cycle: %d checked | %s | worst deltas: %s",
            len(results),
            ", ".join(f"{v}={c}" for v, c in sorted(counts.items())),
            [
                f"{r['key']} ws={r['ws_lp']} rest={r['rest_lp']} (+{r['delta_pct']}%)"
                for r in sorted(divergent, key=lambda x: -x["delta_pct"])[:5]
            ] or "none",
        )
        return results


def run_shadow_loop(api, feed, instruments, interval_sec=30.0, stop_event=None):
    """Blocking loop for a daemon thread; logs one comparison cycle per interval."""
    validator = ShadowValidator(api, feed)
    while True:
        try:
            validator.run_cycle(instruments)
        except Exception as exc:
            log.error("SHADOW cycle failed: %s", exc, exc_info=True)
        if stop_event is not None:
            stop_event.wait(interval_sec)
        else:
            time.sleep(interval_sec)
