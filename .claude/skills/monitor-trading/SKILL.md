# Skill: monitor-trading

Monitor all tmux panes in the `trading` session for errors, attempt code fixes, and restart affected services.

## Pane Map

| Pane | Title | Working Dir | Restart Command |
|------|-------|-------------|-----------------|
| `trading:proxy.0` | broker_proxy | `~/git/trading/shoonya-auth` | `python broker_proxy.py` |
| `trading:proxy.1` | regimetrader | `~/git/trading/regimetrader` | `BROKER_PROXY_URL=http://127.0.0.1:7890 python main.py` |
| `trading:proxy.2` | flowTrader | `~/git/trading/flowTrader` | `BROKER_PROXY_URL=http://127.0.0.1:7890 python main.py` |
| `trading:proxy.3` | portfolio-advisor | `~/git/trading/portfolio-advisor` | `BROKER_PROXY_URL=http://127.0.0.1:7890 python main.py` |
| `trading:proxy.4` | bsensearb | `~/git/trading/bsensearb` | `BROKER_PROXY_URL=http://127.0.0.1:7890 python main.py` |

## Monitoring Steps (run every iteration)

### Step 1 — Confirm the session is still alive
```bash
tmux has-session -t trading 2>/dev/null && echo "ALIVE" || echo "DEAD"
```
If the session is DEAD, report it to the user and **stop the loop** — do not try to restart start.sh automatically.

### Step 2 — Capture the last 200 lines from each pane
For each pane index 0–4:
```bash
tmux capture-pane -t trading:proxy.<N> -p -S -200
```

### Step 3 — Detect errors in captured output

Look for any of these patterns (case-insensitive where noted):
- `Traceback (most recent call last)`
- `^ERROR` or `\bERROR\b` (not in normal log lines like `INFO`)
- `Exception:` or `XceptionError:`
- `CRITICAL`
- `ConnectionRefusedError` / `ConnectionError`
- `[Errno` (socket/OS errors)
- `KeyError` / `AttributeError` / `TypeError` / `ValueError` in a traceback context
- `Killed` or `Segmentation fault`
- A pane that is completely blank or shows only a shell prompt (the process died silently)

**False positives to ignore:**
- Lines that contain `except` or `raise` as Python keywords in code being printed
- Log lines that mention an error in a handled/recovered way (e.g., `Handled ValueError, retrying...`)

### Step 4 — For each pane with errors

#### 4a. Identify the source file
- Look at the traceback to find the file path (e.g., `File "/Users/arshdeep/git/regimetrader/strategy.py", line 42`)
- The last file listed in the traceback is the one that threw the error

#### 4b. Attempt a code fix
- Read the failing file with the Read tool
- Understand the error from the traceback message
- Apply the minimal fix with the Edit tool
- Do NOT refactor, add features, or change logic beyond fixing the immediate error
- If the error is ambiguous, unclear, or could affect trading safety (e.g., wrong position sizing, order logic), **do not auto-fix** — report to the user and restart anyway

#### 4c. Restart the pane
After fixing (or if the error is not code-fixable, e.g., ConnectionRefusedError):
1. Kill the current pane content:
```bash
tmux send-keys -t trading:proxy.<N> C-c
sleep 1
tmux send-keys -t trading:proxy.<N> C-c
```
2. Send the restart command:
```bash
tmux send-keys -t trading:proxy.<N> "cd <WORKING_DIR> && <RESTART_COMMAND>" Enter
```
3. Wait 5 seconds and re-capture the pane to confirm it started without immediately crashing

### Step 5 — Report

After checking all panes, output a brief status summary:
```
🕐 [HH:MM] Trading monitor check
  ✅ broker_proxy — OK
  ⚠️  regimetrader — ERROR detected: <one-line summary>
      → Fixed: <what was changed> | Restarted
  ✅ flowTrader — OK
  ✅ portfolio-advisor — OK
```

If nothing was wrong, a single line suffices:
```
🕐 [HH:MM] All 4 trading panes healthy ✅
```

## Special Cases

- **broker_proxy (pane 0) is down**: This will cascade to all other panes. Fix/restart broker_proxy first, wait for its `/health` endpoint to respond, then check the others.
  ```bash
  curl -sf http://127.0.0.1:7890/health
  ```
- **portfolio-advisor errors**: This pane is advisory/read-only. Restart it but do not block on it or treat it as critical.
- **Repeated crash (same pane crashes again within the same check cycle after restart)**: Report to the user and do NOT restart a third time in the same cycle. Leave it for human review.
