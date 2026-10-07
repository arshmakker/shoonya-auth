# Skill: monitor-trading

Alert-driven monitoring of the `trading` tmux session. A script does the checks; Claude is woken only by an alert, so a quiet day costs no tokens. Do NOT poll panes on a timer.

**Note:** Claude Code runs directly on the droplet (no SSH wrapper). Times in regimetrader logs are UTC; market logic is IST.

## Start (session open, session RUNNING)
Start the watcher under the Monitor tool (each stdout line is an event):
```
cd ~/git/trading/shoonya-auth && python3 -u tools/monitor_trading.py --watch --interval 60 2>&1
```
Monitor expires after 30 min max: re-arm on the expiry notice. Add `--restart-proxy` only if the user asks: it relaunches a DEAD broker_proxy (never regimetrader) in market hours, and only when the engine state is flat now AND the watcher's last good broker check (<10 min old) was flat; max 1 per 10 min, 2 per day. After relaunch it reconciles with the broker once `/health` is up.
On-demand status: `python3 tools/monitor_trading.py --once` (prints `all checks OK` or the active alerts).

## What the script checks (60s; positions every 5 min)
tmux session, pane process alive, broker_proxy `/health` + WS `/feed/status`, regimetrader log freshness (market hours), new error lines (regimetrader log by byte offset, broker_proxy pane; known noise ignored), engine-vs-broker position match, per-IC mark vs carry-stop level / armed floor, and a one-line `DIGEST` at 15:45 IST. It prints only on change: `ALERT <key>: …`, `OK <key>: recovered`.

## When an alert arrives
1. Read only the relevant log slice (`tail`/`grep` on `~/git/trading/regimetrader/logs/ic_system_<date>.log`, or the pane), not 200 lines of both panes.
2. `*.process` dead: report it and stop; never relaunch a pane yourself. regimetrader restarts are always manual (a restart wipes `tracker_positions`). A broker_proxy relaunch is done only by the script's `--restart-proxy` gate; if you see `broker_proxy.relaunch: NOT relaunching ...` the book is not confirmed flat, so tell the user and they restart it. After any `broker_proxy.relaunched` alert, ask the user to verify live-vs-paper mode, WS feed and login, and note the chain/MCX WS subscribe scripts were not re-run. Pane map: pane 0 = broker_proxy, pane 1 = regimetrader.
3. `positions` engine≠broker: report to the user, do not auto-fix.
4. `ic.marks`: informational; the bot's own stops decide. Report, do not act on positions.
5. `error:` lines: identify the file from the traceback. Do not auto-edit code while LIVE (real money); report with the likely cause and let the user decide.

## Never
Never restart regimetrader, never relaunch any pane outside the script's gate, never edit trading code unprompted, never touch `open_positions.json`/`slot_state.json`/`stuck_legs.json`/`pnl_snapshot.json`.
