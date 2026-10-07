#!/usr/bin/env python3
"""Silent-unless-changed watcher for the trading session (no LLM, no tokens).

    python tools/monitor_trading.py --watch            # loop, prints ONLY on state change
    python tools/monitor_trading.py --once             # one full status report
    python tools/monitor_trading.py --watch --restart-proxy  # also relaunch a DEAD broker_proxy, flat book only

Designed to run under Claude Code's Monitor tool: every stdout line is an event
that wakes the session, so a healthy day prints nothing. Lines:
    ALERT <key>: <detail>      a problem appeared or changed
    OK    <key>: recovered     a previously reported problem cleared
    DIGEST ...                 one line at 15:45 IST

It never edits code. regimetrader is NEVER restarted (a restart wipes
tracker_positions). A dead broker_proxy is relaunched only with --restart-proxy,
in market hours, and only if the book is flat per BOTH the engine state file now
AND a recent successful broker check (the proxy is down, so the broker cannot be
asked live). After a relaunch it reconciles against the broker as soon as /health
is up. Otherwise a dead pane is just reported.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = "trading:proxy"
HOME = Path.home() / "git/trading"
RT = HOME / "regimetrader"
PROXY = "http://127.0.0.1:7890"
POS_FILE = RT / "data/open_positions.json"
PNL_FILE = RT / "data/pnl_snapshot.json"
LOT = 65
CARRY_STOP_PER_LOT = 600.0  # mirrors settings.IC_CARRY_STOP_PER_LOT; alert level only
LOG_FRESH_SEC = 150  # main loop logs every 30s
POS_EVERY = 300  # broker positions check cadence (REST call)
PANES = {0: "broker_proxy", 1: "regimetrader"}
# Must match start_vps.sh (the proxy's env: WS feed mode, shutdown time, tick persist).
# The chain/MCX WS subscribe scripts are NOT re-run; the relaunch alert says so.
PROXY_CMD = (f"cd {HOME / 'shoonya-auth'} && SHOONYA_FEED_MODE=ws SHOONYA_SHADOW_INTERVAL=300 "
             f"SHOONYA_SHUTDOWN_TIME=23:58 SHOONYA_TICK_PERSIST_DIR='{RT}' ./venv/bin/python broker_proxy.py")
BROKER_SNAP_MAX_AGE = 600  # last good broker check must be this fresh to count as "flat per broker"
RELAUNCH_COOLDOWN = 600
RELAUNCH_MAX_PER_DAY = 2
SHELLS = {"zsh", "bash", "sh", "fish"}
ERROR_RE = re.compile(r"Traceback \(most recent call last\)|\bERROR\b|\bCRITICAL\b|Exception:|\[Errno|"
                      r"Segmentation fault|\bKilled\b")
# Handled/expected noise (CLAUDE.md + memory): cross-wired quotes are guarded, REST
# timeouts are expected until REST is dropped, "tracker had no entry records" is the
# known restart approximation, and the LIVE trail logs WARNING lines routinely.
IGNORE_RE = re.compile(r"cross-wired respon|REST fallback|timed? ?out|tracker had no entry records|"
                       r"not corroborated|TRAIL LOSS OVERRIDE|werkzeug", re.I)
SEEN = Path("/tmp/trading_monitor_seen.json")


def now_ist() -> datetime:
    return datetime.now(IST)


def live(now: datetime) -> bool:
    o = now.replace(hour=9, minute=15, second=0, microsecond=0)
    c = now.replace(hour=15, minute=40, second=0, microsecond=0)
    return now.weekday() < 5 and o <= now <= c


def sh(*a: str) -> str:
    return subprocess.run(a, capture_output=True, text=True, timeout=20).stdout


def http_json(url: str, data: dict | None = None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


class Watcher:
    def __init__(self, restart_proxy: bool = False):
        self.restart_proxy = restart_proxy
        self.broker_snap: tuple[float, dict[str, int]] | None = None  # (time, last good broker positions)
        self.relaunches: list[float] = []
        self.post_relaunch = False
        self.active: dict[str, str] = {}  # key -> last reported detail
        self.log_offset: dict[str, int] = {}
        self.pane_seen: set[str] = self._load_seen()
        self.last_pos_check = 0.0
        self.digest_day = ""

    # -- persistence of already-reported error lines (survives a watcher restart)
    @staticmethod
    def _load_seen() -> set[str]:
        try:
            d = json.loads(SEEN.read_text())
            if d.get("date") == now_ist().strftime("%Y-%m-%d"):
                return set(d["seen"])
        except Exception:
            pass
        return set()

    def _save_seen(self) -> None:
        try:
            SEEN.write_text(json.dumps({"date": now_ist().strftime("%Y-%m-%d"), "seen": sorted(self.pane_seen)}))
        except Exception:
            pass

    # -- state-change reporting
    def set(self, key: str, detail: str | None, out: list[str]) -> None:
        prev = self.active.get(key)
        if detail is None:
            if prev is not None:
                del self.active[key]
                out.append(f"OK    {key}: recovered")
        elif detail != prev:
            self.active[key] = detail
            out.append(f"ALERT {key}: {detail}")

    # -- individual checks; each returns detail string on problem, else None
    def pane_dead(self, idx: int) -> str | None:
        for line in sh("tmux", "list-panes", "-t", SESSION, "-F", "#{pane_index}|#{pane_current_command}|#{pane_dead}").splitlines():
            i, cmd, dead = line.split("|")
            if int(i) == idx:
                return f"process exited (now: {cmd})" if dead == "1" or cmd in SHELLS else None
        return "pane missing"

    def new_errors_log(self, path: Path) -> list[str]:
        """Error lines appended to a log file since the last look (byte offset; no re-reads)."""
        try:
            size = path.stat().st_size
        except OSError:
            return []
        off = self.log_offset.get(str(path))
        self.log_offset[str(path)] = size
        if off is None or size < off:  # first look / rotated: start from here, don't replay history
            return []
        with open(path, errors="replace") as f:
            f.seek(off)
            chunk = f.read()
        return self._filter(chunk.splitlines())

    def new_errors_pane(self, idx: int) -> list[str]:
        lines = sh("tmux", "capture-pane", "-t", f"{SESSION}.{idx}", "-p", "-S", "-100").splitlines()
        return self._filter(lines)

    def _filter(self, lines: list[str]) -> list[str]:
        hits = []
        for l in lines:
            if ERROR_RE.search(l) and not IGNORE_RE.search(l):
                key = re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+", "", l).strip()
                if key not in self.pane_seen:
                    self.pane_seen.add(key)
                    hits.append(l.strip()[:180])
        return hits

    @staticmethod
    def engine_positions() -> dict[str, int]:
        d = json.loads(POS_FILE.read_text())
        want: dict[str, int] = {}
        for st in (d.get("strategies") or {}).values():
            p = st.get("position")
            if not p:
                continue
            q = p["lots"] * LOT
            for sym, sign, closed in (("sc_sym", -1, "call_spread_closed"), ("lc_sym", 1, "call_spread_closed"),
                                      ("sp_sym", -1, "put_spread_closed"), ("lp_sym", 1, "put_spread_closed")):
                if not p.get(closed):
                    s = p[sym].split("|", 1)[-1]
                    want[s] = want.get(s, 0) + sign * q
        return {k: v for k, v in want.items() if v}

    @staticmethod
    def broker_positions() -> dict[str, int]:
        d = http_json(f"{PROXY}/call", {"method": "get_positions"})
        if not isinstance(d, list):
            raise RuntimeError(f"get_positions: {str(d)[:80]}")
        return {p["tsym"]: int(p["netqty"]) for p in d if int(p.get("netqty", 0)) != 0}

    @staticmethod
    def ic_marks() -> list[str]:
        """Alert text for any open IC at/below the carry-stop level or at/below its armed floor."""
        d = json.loads(POS_FILE.read_text())
        out = []
        for sid, st in (d.get("strategies") or {}).items():
            p, lm = st.get("position"), st.get("leg_marks") or {}
            if not p or p.get("call_spread_closed") or p.get("put_spread_closed"):
                continue
            try:
                m = {k: float(lm[p[f"{k}_sym"]]["ltp"]) for k in ("sc", "sp", "lc", "lp")}
            except (KeyError, TypeError, ValueError):
                out.append(f"{sid}: leg marks missing")
                continue
            pnl = (p["entry_credit"] - (m["sc"] + m["sp"] - m["lc"] - m["lp"])) * p["lots"] * LOT
            stop = -CARRY_STOP_PER_LOT * p["lots"]
            if pnl <= stop:
                out.append(f"{sid}: ltp_pnl {pnl:+,.0f} <= {stop:,.0f} ({CARRY_STOP_PER_LOT:.0f}/lot)")
            elif p.get("trail_tier_armed") and p.get("trail_floor_retain_frac"):
                floor = p["peak_pnl"] * p["trail_floor_retain_frac"]
                if pnl <= floor:
                    out.append(f"{sid}: armed, ltp_pnl {pnl:+,.0f} <= floor {floor:,.0f} (peak {p['peak_pnl']:,.0f})")
        return out

    def maybe_relaunch_proxy(self, now: datetime, out: list[str]) -> None:
        """Relaunch broker_proxy only on a flat book (engine now + recent broker check). Never regimetrader."""
        t = time.time()
        recent = [r for r in self.relaunches if t - r < 86400]
        if recent and t - recent[-1] < RELAUNCH_COOLDOWN:
            return
        why = None
        if len(recent) >= RELAUNCH_MAX_PER_DAY:
            why = f"relaunch limit reached ({RELAUNCH_MAX_PER_DAY}/day)"
        else:
            try:
                eng = self.engine_positions()
            except Exception as e:
                eng, why = {}, f"engine state unreadable ({e.__class__.__name__})"
            if why is None and eng:
                why = f"engine not flat ({len(eng)} legs open)"
            elif why is None and (self.broker_snap is None or t - self.broker_snap[0] > BROKER_SNAP_MAX_AGE):
                why = f"no broker check in the last {BROKER_SNAP_MAX_AGE // 60} min (proxy down; cannot confirm flat)"
            elif why is None and self.broker_snap[1]:
                why = f"last broker check NOT flat ({len(self.broker_snap[1])} legs)"
        if why:
            self.set("broker_proxy.relaunch", f"NOT relaunching broker_proxy: {why}. Manual restart needed", out)
            return
        sh("tmux", "send-keys", "-t", f"{SESSION}.0", PROXY_CMD, "Enter")
        self.relaunches.append(t)
        self.post_relaunch = True
        self.last_pos_check = 0.0
        self.set("broker_proxy.relaunch", None, out)
        self.set("broker_proxy.relaunched", f"relaunched at {now:%H:%M} IST on a flat book. VERIFY: mode (live/paper), WS feed, "
                 "login; chain/MCX WS subscribe scripts were NOT re-run. regimetrader untouched", out)

    # -- one pass
    def cycle(self) -> list[str]:
        out: list[str] = []
        now = now_ist()
        is_live = live(now)

        if subprocess.run(["tmux", "has-session", "-t", "trading"], capture_output=True).returncode:
            self.set("tmux", "session 'trading' is DEAD", out)
            return out
        self.set("tmux", None, out)

        for idx, name in PANES.items():
            dead = self.pane_dead(idx)
            self.set(f"{name}.process", dead, out)
            if dead and idx == 0 and self.restart_proxy and is_live:
                self.maybe_relaunch_proxy(now, out)

        try:
            ok = http_json(f"{PROXY}/health").get("ok") is True
            self.set("proxy.health", None if ok else "/health not ok", out)
            fs = http_json(f"{PROXY}/feed/status")
            bad = None
            if fs.get("enabled") and (not fs.get("connected") or (fs.get("last_msg_age_sec") or 0) > 30 and is_live):
                bad = f"WS connected={fs.get('connected')} last_msg_age={fs.get('last_msg_age_sec')}s"
            self.set("proxy.feed", bad, out)
        except Exception as e:
            self.set("proxy.health", f"unreachable ({e.__class__.__name__})", out)

        log = RT / f"logs/ic_system_{now:%Y%m%d}.log"
        if is_live:
            try:
                age = time.time() - log.stat().st_mtime
                self.set("regimetrader.log", None if age < LOG_FRESH_SEC else f"log silent {age:.0f}s (hung?)", out)
            except OSError:
                self.set("regimetrader.log", f"no log file for today ({log.name})", out)

        errs = self.new_errors_log(log) + self.new_errors_pane(0)
        for e in errs[-3:]:
            out.append(f"ALERT error: {e}")
        if errs:
            self._save_seen()

        try:
            marks = self.ic_marks() if is_live else []
        except Exception as e:
            marks = [f"state unreadable ({e.__class__.__name__})"]
        self.set("ic.marks", "; ".join(marks) or None, out)

        proxy_up = "proxy.health" not in self.active
        if is_live and time.time() - self.last_pos_check >= POS_EVERY and (proxy_up or not self.post_relaunch):
            self.last_pos_check = time.time()
            try:
                eng, brk = self.engine_positions(), self.broker_positions()
                self.broker_snap = (time.time(), brk)
                if self.post_relaunch:  # first clean broker read after a relaunch
                    self.post_relaunch = False
                    self.set("broker_proxy.relaunched", None, out)
                diff = {k: (eng.get(k, 0), brk.get(k, 0)) for k in set(eng) | set(brk) if eng.get(k, 0) != brk.get(k, 0)}
                self.set("positions", ("engine≠broker " + ", ".join(f"{k} eng={a} brk={b}" for k, (a, b) in sorted(diff.items()))) if diff else None, out)
            except Exception as e:
                self.set("positions", f"check failed ({e.__class__.__name__}: {str(e)[:60]})", out)

        day = now.strftime("%Y-%m-%d")
        if now.weekday() < 5 and now.strftime("%H:%M") >= "15:45" and self.digest_day != day:
            self.digest_day = day
            try:
                s = json.loads(PNL_FILE.read_text())
                d = s.get("daily", {})
                out.append(f"DIGEST {day}: daily {d.get('realised_pnl', 0):+,.0f} ({d.get('trades', 0)} trades), "
                           f"cumulative {s.get('realised_pnl', 0):+,.0f}, open ICs {len(self.engine_positions())} legs")
            except Exception as e:
                out.append(f"DIGEST {day}: unreadable ({e.__class__.__name__})")
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="single check, always prints a status report")
    g.add_argument("--watch", action="store_true", help="loop; print only on state change")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--restart-proxy", action="store_true",
                    help="relaunch a DEAD broker_proxy (market hours, flat book only); never regimetrader")
    a = ap.parse_args()
    w = Watcher(a.restart_proxy)
    if a.once:
        w.last_pos_check = 0.0
        lines = w.cycle()
        print("\n".join(lines) if lines else f"{now_ist():%H:%M IST} all checks OK")
        return int(bool(w.active))
    while True:  # first pass sets log offsets, so history is not replayed
        try:
            for line in w.cycle():
                print(line, flush=True)
        except Exception as e:  # a watcher must not die on its own bug
            print(f"ALERT monitor.bug: {e!r}"[:200], flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
