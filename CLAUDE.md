# shoonya-auth — Claude Code Instructions

## Session Start: Trading Monitor

**Every time a session opens in this directory, immediately and automatically:**

### Step 1 — Check if start.sh has been run
```bash
tmux has-session -t trading 2>/dev/null && echo "RUNNING" || echo "NOT STARTED"
```

**If NOT STARTED:**
- Report: "⚪ Trading session not started. Run `./start.sh` when ready."
- Wait for user instructions. Do NOT start monitoring.

**If RUNNING:**
- Confirm which panes are alive:
  ```bash
  tmux list-panes -t trading:proxy -F "Pane #{pane_index}: #{pane_title} (#{pane_pid})"
  ```
- Report the status briefly, then start the alert-driven watcher under the Monitor tool (no polling loop; it prints only on state change, so quiet days cost no tokens):

  ```
  cd ~/git/trading/shoonya-auth && python3 -u tools/monitor_trading.py --watch --interval 60 2>&1
  ```
  Re-arm it when Monitor expires (30 min max). See `.claude/skills/monitor-trading/SKILL.md`.

### What the monitor does
- Checks process liveness, broker_proxy `/health` and WS feed, regimetrader log freshness, new error lines, engine-vs-broker positions, and IC marks vs stops
- Alerts only on change (`ALERT`/`OK recovered`), plus a `DIGEST` line at 15:45 IST
- Never edits code and never restarts regimetrader. With `--restart-proxy` it relaunches a DEAD broker_proxy only on a broker-confirmed flat book (market hours, 10 min cooldown, 2/day); otherwise a dead pane is reported and restarts are manual

---

## Project Overview
This repo provides centralized OAuth login and a broker proxy for the Shoonya trading API.
All trading projects live under `~/git/trading/`.

### Services (started by start.sh)
| Service | Pane | Port | Path |
|---------|------|------|------|
| broker_proxy | proxy.0 | 7890 | `~/git/trading/shoonya-auth` |
| regimetrader | proxy.1 | — | `~/git/trading/regimetrader` |

Note: flowTrader, portfolio-advisor, and bsensearb are not part of this session's monitoring scope.

### Credentials
- Stored at `~/.shoonya/cred.yml` (never in this repo)
- Template: `cred.yml.template`
