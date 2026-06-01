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
- Report the status briefly, then immediately start the monitoring loop:

  ```
  /loop 10m monitor-trading
  ```

  This will invoke the `monitor-trading` skill every 10 minutes to check all panes for errors, attempt code fixes, and restart any crashed services.

### What the monitor does each cycle
- Captures the last 200 lines from all 4 panes (broker_proxy, regimetrader, flowTrader, portfolio-advisor)
- Detects Python errors, tracebacks, and silent crashes
- Attempts minimal code fixes for clear errors (ImportError, SyntaxError, etc.)
- Restarts the affected pane
- Reports a one-line status per pane

See `.claude/skills/monitor-trading/SKILL.md` for full details.

---

## Project Overview
This repo provides centralized OAuth login and a broker proxy for the Shoonya trading API.

### Services (started by start.sh)
| Service | Pane | Port |
|---------|------|------|
| broker_proxy | proxy.0 | 7890 |
| regimetrader | proxy.1 | — |
| flowTrader | proxy.2 | — |
| portfolio-advisor | proxy.3 | — |

### Credentials
- Stored at `~/.shoonya/cred.yml` (never in this repo)
- Template: `cred.yml.template`
