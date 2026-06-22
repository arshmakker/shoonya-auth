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
For each pane index 0–4:
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

## bsensearb (pane 4) — LIVE TRADING SAFETY RULES

**bsensearb is live (10L capital, real orders). Apply stricter rules than other panes:**

### Order errors → STOP immediately, do NOT restart
If any of the following appear in the bsensearb pane output after the last "STARTING" banner:
- Any line containing `place_order` alongside `ERROR`, `Exception`, `failed`, or `rejected`
- Any line containing `order` and `Traceback`
- Log lines indicating unexpected order state: `duplicate order`, `insufficient funds`, `margin`, `RMS`, `OMS`
- Any `CRITICAL` log line **except** sell-timeout CRITICALs (see "Normal warnings to ignore" below)
- Repeated `timeout` on order status checks (more than 3 consecutive timeouts logged)

**Action: STOP bsensearb immediately — do NOT restart.**
```bash
tmux send-keys -t trading:proxy.4 C-c
sleep 1
tmux send-keys -t trading:proxy.4 C-c
```
Then alert the user with a clear message:
```
🚨 bsensearb STOPPED — order error detected: <exact log line>
   Manual review required before restarting.
```

### Code/infra errors → STOP, report, do NOT auto-restart
If bsensearb has a Python traceback or connection error (not order-related):
- **Do NOT auto-restart** (unlike other panes)
- Stop the process with C-c
- Report the error to the user and wait for explicit approval to restart

### Normal warnings to ignore
- `WARNING - No symbols passed ADV/value filters` — harmless, fallback to Nifty50
- `WARNING - Top-active filter returned no symbols` — harmless fallback
- Short leg quote unavailable warnings — pre-open, not bsensearb
- Collection cycle complete lines — healthy operation
- `CRITICAL - SELL TIMEOUT` followed by `CRITICAL - Emergency sell price` and `CRITICAL - Emergency aggressive-limit sell placed` — this is the designed timeout handler firing (cancel stale DAY order + place IOC limit). Report it as a warning in the status summary but do NOT stop bsensearb. Only escalate to STOP if the emergency sell itself errors (e.g. `Emergency sell` alongside `failed`, `rejected`, or `Exception`).
