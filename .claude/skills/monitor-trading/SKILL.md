# Skill: monitor-trading

Monitor the broker_proxy and regimetrader tmux panes in the `trading` session for errors, attempt code fixes, and restart affected services.

## Pane Map

| Pane | Title | Working Dir | Restart Command |
|------|-------|-------------|-----------------|
| `trading:proxy.0` | broker_proxy | `~/git/trading/shoonya-auth` | `python broker_proxy.py` |
| `trading:proxy.1` | regimetrader | `~/git/trading/regimetrader` | `BROKER_PROXY_URL=http://127.0.0.1:7890 python main.py` |

## Monitoring Steps (run every iteration)

### Step 0 — Check market hours
```bash
python3 -c "
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
market_close = now.replace(hour=15, minute=40, second=0, microsecond=0)
is_weekend = now.weekday() >= 5
within_hours = not is_weekend and market_open <= now <= market_close
print('WITHIN_HOURS' if within_hours else 'AFTER_HOURS')
print(now.strftime('%H:%M IST'))
"
```

**If AFTER_HOURS:** Do NOT restart any pane — processes are expected to have exited. Only report their status. Skip Steps 4c (restart). Still capture panes and detect errors (for next morning awareness), but end the report with: `⏰ After market hours — no restarts attempted.`

### Step 1 — Confirm the session is still alive
```bash
tmux has-session -t trading 2>/dev/null && echo "ALIVE" || echo "DEAD"
```
If the session is DEAD, report it to the user and **stop the loop** — do not try to restart start.sh automatically.

### Step 2 — Capture the last 200 lines from each pane
For each pane index 0–1:
```bash
tmux capture-pane -t trading:proxy.<N> -p -S -200
```

### Step 3 — Detect errors in captured output

**IMPORTANT — scope to current run only:** The pane buffer may contain output from previous runs (e.g., a `KeyboardInterrupt` or `Traceback` from the run that was killed to restart the process). Before scanning for errors, find the LAST occurrence of a "STARTING" or "Starting" banner (e.g., `=== PCR CREDIT SPREAD SYSTEM STARTING ===`, `=== Starting Trading System ===`, `broker_proxy starting`, etc.) in the captured lines. Only scan lines AFTER that banner. If no banner is found, scan all lines.

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
- Any `Traceback` / `KeyboardInterrupt` / error lines that appear BEFORE the last "STARTING" banner — these are from prior runs and must be ignored
- A pane that shows a healthy startup banner followed by normal INFO log lines, even if the last log line is several minutes old — silence is normal when no trades or alerts are pending (verify with `kill -0 <pid>` from the PID file before declaring dead)

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

### Step 4d — Check regimetrader P&L (WITHIN_HOURS only)

Only regimetrader (the strategy in this session's monitoring scope) has a live P&L to check — broker_proxy has none.

1. Find today's IC entry:
```bash
grep "IC.*ENTERED" ~/git/trading/regimetrader/logs/ic_system_$(date +%Y%m%d).log | tail -1
```
If no entry found, report "No IC entered today" and skip the rest of this step.

2. Compute estimated unrealized P&L using the same method as the `unrealized-pnl` skill, Step 2 (quote-book mid / last-valid prices per leg, avg= entry prices, short/long P&L formula). Also note the entry `Credit=<pts> × qty` as `credit_value` (total credit collected in ₹).

3. Evaluate against both thresholds — flag if EITHER trips:
   - **Credit-erosion**: unrealized loss > 50% of `credit_value`
   - **Fixed floor**: unrealized P&L < −₹5,000

   (These are defaults — adjust here if the user gives different numbers.)

4. If a threshold trips, add a suggestion to the report (do NOT auto-act on positions — this is a recommendation only):
   - Loss > 50% of credit → suggest considering an early exit/adjustment of the IC (standard risk-mgmt threshold for credit spreads)
   - Loss < −₹5,000 → suggest reviewing the position for a manual stop-out
   - If a leg price is missing/stale, flag that the P&L estimate is incomplete before suggesting any action

### Step 5 — Report

After checking all panes, output a brief status summary:
```
🕐 [HH:MM] Trading monitor check
  ✅ broker_proxy — OK
  ⚠️  regimetrader — ERROR detected: <one-line summary>
      → Fixed: <what was changed> | Restarted
  📊 regimetrader P&L: −₹6,200 (Credit=22.60pts, 10 lots) ⚠️ exceeds −₹5,000 floor
      → Suggest: review IC for manual stop-out; loss is 82% of credit collected
```

If nothing was wrong and P&L is within thresholds, a single line suffices:
```
🕐 [HH:MM] Both trading panes healthy ✅ | regimetrader P&L: +₹1,850 (within thresholds)
```

If AFTER_HOURS or no IC entered today, omit the P&L line (or state "No IC entered today").

## Special Cases

- **broker_proxy (pane 0) is down**: This will cascade to regimetrader. Fix/restart broker_proxy first, wait for its `/health` endpoint to respond, then check regimetrader.
  ```bash
  curl -sf http://127.0.0.1:7890/health
  ```
- **Repeated crash (same pane crashes again within the same check cycle after restart)**: Report to the user and do NOT restart a third time in the same cycle. Leave it for human review.
