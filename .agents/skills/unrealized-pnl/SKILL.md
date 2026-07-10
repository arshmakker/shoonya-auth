# Skill: unrealized-pnl

Report estimated unrealized P&L for all active trading strategies by reading today's log files. No live API calls — log files only.

## Step 1 — Locate today's log files

```bash
DATE=$(date +%Y%m%d)
RT_LOG=~/git/trading/regimetrader/logs/ic_system_${DATE}.log
FT_LOG=~/git/trading/flowTrader/logs/pcs_${DATE}.log
ARB_LOG=~/git/trading/bsensearb/logs/trading_system_${DATE}.log
ARB_BOOK=~/git/trading/bsensearb/arbitrage_paper_book.json
```

Check each exists:
```bash
[ -f $RT_LOG ] && echo "RT ok" || echo "RT not started"
[ -f $FT_LOG ] && echo "FT ok" || echo "FT not started"
[ -f $ARB_LOG ] && echo "ARB ok" || echo "ARB not started"
```

---

## Step 2 — regimetrader (Iron Condor)

### Entry info
```bash
grep "IC.*ENTERED" ~/git/trading/regimetrader/logs/ic_system_$(date +%Y%m%d).log | tail -1
```
Parse: `SC=<price> SP=<price> LC=<price> LP=<price> | Credit=<pts> | Lots=<n> (LotSize=<ls>)`
→ qty = lots × lot_size

If no IC entry found today, report "No IC entered today".

### Last known price per leg
Prefer `quote-book mid` lines (from paper_position_tracker) — more reliable than LTP fallbacks.
Fall back to `last valid` lines (from suspicious LTP warnings).

```bash
# Get last known price for each leg — use the most recent line per strike
grep -E "quote-book mid|last valid" ~/git/trading/regimetrader/logs/ic_system_$(date +%Y%m%d).log | tail -40
```

For each of the 4 strikes (SC, SP, LC, LP), find the most recent price line mentioning that strike number.

### SC entry price
The IC ENTERED line doesn't log individual leg entry prices. Estimate from the Credit and the other legs:
- Use entry prices from the `paper_position_tracker` avg= fields if available in WARNING lines:
  ```bash
  grep "excluded from mark\|avg=" ~/git/trading/regimetrader/logs/ic_system_$(date +%Y%m%d).log | tail -20
  ```
  Format: `no price for NFO|NIFTY...C24600 (qty=-650, avg=21.90)`
  → avg IS the entry price per leg

### P&L calculation
- Short legs (SC, SP): `profit = (avg_entry − current_price) × qty`
- Long legs (LC, LP): `profit = (current_price − avg_entry) × qty`  (decay = profit for longs)
- Total unrealized = sum of all four legs
- If a leg has no current price: exclude it from total and flag it ⚠️

---

## Step 3 — flowTrader (PCR Credit Spread)

### Entry info
```bash
grep "PAPER ORDER\|Entered BEAR\|Entered BULL" ~/git/trading/flowTrader/logs/pcs_$(date +%Y%m%d).log
```
- `PAPER ORDER S NFO|NIFTY...C<strike> <qty> @ <price>` → short leg entry
- `PAPER ORDER B NFO|NIFTY...C<strike> <qty> @ <price>` → long leg entry
- Fees logged inline: `(fees=₹<total> ...)`

### Last known price per leg
```bash
grep "last valid" ~/git/trading/flowTrader/logs/pcs_$(date +%Y%m%d).log | tail -20
```
Pick the most recent line per option symbol.

### P&L calculation (Bear Call example)
- `profit = (short_entry − short_current) × qty − (long_current − long_entry) × qty − total_entry_fees`
- For Bull Put: same formula with put strikes

### Prior realised state
```bash
grep "Restored P&L state" ~/git/trading/flowTrader/logs/pcs_$(date +%Y%m%d).log | tail -1
```
Report this as context (e.g. cumulative loss from prior days).

If no position entered today, report "No spread entered today".

---

## Step 4 — bsensearb (NSE-BSE Arbitrage)

### Paper book (most reliable — reads JSON directly)
```bash
python3 -c "
import json
with open(os.path.expanduser('~/git/trading/bsensearb/arbitrage_paper_book.json')) as f:
    d = json.load(f)
last_trade = d['trades'][-1]['timestamp'] if d['trades'] else 'none'
print(f'capital={d[\"current_capital\"]:.2f}')
print(f'cumulative_pnl={d[\"cumulative_pnl\"]:.2f}')
print(f'daily_pnl={d[\"daily_pnl\"]:.2f}')
print(f'total_trades={len(d[\"trades\"])}')
print(f'last_reset={d[\"last_reset_date\"]}')
print(f'last_trade={last_trade}')
"
```

### Today's activity
```bash
grep "filled qty\|paper book\|rejected\|Arbitrage:" ~/git/trading/bsensearb/logs/trading_system_$(date +%Y%m%d).log | tail -10
```

Note if daily_pnl reset date != today → daily P&L counter is stale (not reset this session).
Note if all today's orders rejected with T5 block → daily P&L will be 0.

---

## Step 5 — Report

Output a clean summary in this format:

```
📊 Unrealized P&L — HH:MM IST

━━━ regimetrader (IC NIFTY — LIVE) ━━━
  Entry: SC=24600 SP=23500 LC=24950 LP=23150 | Credit=22.60pts | 10 lots (650 qty)

  SC 24600C (short): entry 21.90 → current 18.35 → +₹2,308 ✅
  SP 23500P (short): entry 21.90 → current 22.50 → −₹390  ⚠️
  LC 24950C (long):  entry  8.45 → current  5.50 → +₹1,918 ✅
  LP 23150P (long):  entry 10.75 → current  7.65 → +₹2,015 ✅
  ─────────────────────────────────────────────
  Est. unrealized P&L: +₹5,851
  ⚠️ Note: prices from last-valid LTP (may be 10–30min stale)

━━━ flowTrader (BEAR_CALL NIFTY — PAPER) ━━━
  Short 24200C: sold 65 @ 77.75 → current 93.00 → −₹997 ⚠️
  Long  24400C: bought 65 @ 35.00 → current 41.95 → +₹452 ✅
  Entry fees: −₹19.68
  ─────────────────────────────────────────────
  Est. unrealized P&L: −₹564 (paper only)
  Prior realised (all time): −₹8,216

━━━ bsensearb (NSE-BSE Arb — LIVE) ━━━
  Capital: ₹1,001,005 | Cumulative P&L: +₹1,005 | Daily P&L: ₹0
  Trades in book: 5 (last: 2026-06-09)
  Today: 0 fills — T5 debit block active on account
```

## Key rules
- **Never call the broker API** — read logs and JSON files only
- **Label live vs paper** clearly — regimetrader and bsensearb are live; flowTrader is paper
- **Stale prices**: always note that prices come from last-valid LTP lines and may be stale; do not present estimates as exact
- **Missing leg prices**: if a leg has no price at all, exclude from total and explicitly flag it — do not zero it silently
- **No IC entered**: if regimetrader has no ENTERED line today, say so — do not guess
- **daily_pnl reset**: if bsensearb `last_reset_date` ≠ today, flag that the daily counter hasn't reset this session
