"""Offline temporal validation of the frozen NIFTY low-PCR research rule.

No broker imports, credentials, trading APIs or order submission. Public-data
downloads are a separate command. Run from the repository root with Python 3.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/pcr_validation'
RULE_PATH = OUT / 'frozen_rule.json'
DATA = Path('/private/tmp/pcr_validation_cache')
TRADING = ROOT.parent
COLS = ['TradDt', 'TckrSymb', 'XpryDt', 'StrkPric', 'OptnTp', 'ClsPric',
        'UndrlygPric', 'OpnIntrst', 'TtlNbOfTxsExctd', 'NewBrdLotQty']


def locked_rule():
    raw = RULE_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != RULE_PATH.with_suffix('.sha256').read_text().strip():
        raise ValueError('Frozen rule changed; do not silently retune this validation')
    return json.loads(raw), digest


def signal_for_day(frame, day):
    options = frame[frame.OptnTp.isin(['CE', 'PE'])]
    expiries = sorted(e for e in options.XpryDt.dropna().unique()
                      if 2 <= (pd.Timestamp(e) - day).days <= 8)
    if not expiries:
        return {'status': 'no_eligible_expiry'}
    expiry = pd.Timestamp(expiries[0])
    chain = options[options.XpryDt.eq(expiry)]
    if chain.duplicated(['XpryDt', 'StrkPric', 'OptnTp']).any():
        return {'status': 'duplicate_chain_contracts'}
    puts = chain.loc[chain.OptnTp.eq('PE'), 'OpnIntrst'].sum()
    calls = chain.loc[chain.OptnTp.eq('CE'), 'OpnIntrst'].sum()
    spots = frame.UndrlygPric[frame.UndrlygPric > 0]
    if puts <= 0 or calls <= 0 or spots.empty:
        return {'status': 'missing_signal_inputs'}
    ratio = float(puts / calls)
    underlying = float(spots.median())
    atm = round(underlying / 50) * 50
    return {'status': 'signal' if ratio < 0.7 else 'no_signal',
            'pcr': ratio, 'expiry': str(expiry.date()), 'signal_spot': underlying,
            'short': atm + 200, 'long': atm + 400,
            'put_contracts': int(chain.OptnTp.eq('PE').sum()),
            'call_contracts': int(chain.OptnTp.eq('CE').sum())}


def fee_points(short_price, long_price, short_intrinsic, long_intrinsic, quantity):
    if quantity <= 0:
        raise ValueError('Quantity must be positive')
    # Exact discovery stress formula retained solely to compare like with like.
    turnover = (short_price + long_price + short_intrinsic + long_intrinsic) * quantity
    stress = (23.6 + turnover * (.0003553 + .000001) * 1.18
              + (short_price + long_intrinsic) * quantity * .0015
              + (long_price + short_intrinsic) * quantity * .00003
              + long_intrinsic * quantity * .00125)
    # May–September 2026 entry plus cash expiry estimate: no fictional exit orders.
    # NSE exercise STT is 0.15% from 1 Apr 2026, paid on exercised long intrinsic.
    entry_turnover = (short_price + long_price) * quantity
    settlement = (11.8 + entry_turnover * (.0003553 + .000001) * 1.18
                  + short_price * quantity * .0015 + long_price * quantity * .00003
                  + long_intrinsic * quantity * .0015)
    return stress / quantity, settlement / quantity


def choose_quote(frame, target, side, quantity):
    """Select a later fresh marketable quote; never backfill from future to past."""
    if 'snap_time' not in frame or 'feed_time' not in frame:
        return None
    f = frame.copy()
    f['t'] = pd.to_datetime(f.snap_time, utc=True, format='mixed', errors='coerce')
    feed = pd.to_numeric(f.feed_time, errors='coerce')
    f['age'] = f.t.astype('datetime64[ns, UTC]').astype('int64') / 1e9 - feed
    for c in ['bid', 'ask', 'bid_qty', 'ask_qty']:
        if c not in f:
            return None
        f[c] = pd.to_numeric(f[c], errors='coerce')
    valid = ((f.t >= target) & (f.t <= target + pd.Timedelta(seconds=60))
             & f.age.between(0, 5) & (f.bid > 0) & (f.ask >= f.bid)
             & (f['ask_qty' if side == 'BUY' else 'bid_qty'] >= quantity))
    f = f[valid].sort_values('t')
    if f.empty:
        return None
    r = f.iloc[0]
    return {'time': r.t.isoformat(), 'price': float(r['ask' if side == 'BUY' else 'bid']),
            'age_seconds': float(r.age)}


def quote_audit(record):
    day = record['entry_date'].replace('-', '')
    tag = pd.Timestamp(record['expiry']).strftime('%d%b%y').upper()
    sources = [ROOT/'droplet_backup_20260905/regimetrader', TRADING/'regimetrader']
    frames = {}
    paths = {}
    for leg in ['short', 'long']:
        name = f"NIFTY{tag}C{record[leg]}_{day}.csv"
        candidates = [base/f'market_data_{day}/raw_data/ticks'/name for base in sources]
        f = next((f for f in candidates if f.exists()), None)
        if f is None:
            return {'status': 'missing_recorded_tick_contract', 'missing_leg': leg}
        frames[leg] = pd.read_csv(f)
        paths[leg] = str(f)
    target = pd.Timestamp(record['entry_date'] + ' 15:20:01', tz='Asia/Kolkata').tz_convert('UTC')
    long = choose_quote(frames['long'], target, 'BUY', record['lot_qty'])
    if long is None:
        return {'status': 'no_fresh_depth_for_hedge', 'sources': paths}
    short = choose_quote(frames['short'], pd.Timestamp(long['time']) + pd.Timedelta(seconds=1),
                         'SELL', record['lot_qty'])
    if short is None:
        return {'status': 'hedge_quote_only_short_unavailable', 'hedge': long, 'sources': paths}
    credit = short['price'] - long['price']
    return {'status': 'quoted_entry_feasible' if 0 < credit < 200 else 'invalid_quoted_credit',
            'credit': credit, 'short_quote': short, 'long_quote': long, 'sources': paths,
            'limitation': 'Quoted liquidity is not a fill. Feed age may not be per-side quote age. Expiry exit and failed hedge unwind are not execution-validated.'}


def read_file(path):
    frame = pd.read_csv(path, usecols=COLS, low_memory=False)
    frame = frame[frame.TckrSymb.eq('NIFTY')].copy()
    frame.TradDt = pd.to_datetime(frame.TradDt)
    frame.XpryDt = pd.to_datetime(frame.XpryDt)
    expected = pd.Timestamp(path.name.split('_')[-3])
    if not frame.TradDt.eq(expected).all():
        raise ValueError(f'Archive date mismatch: {path.name}')
    return frame


def run():
    rule, digest = locked_rule()
    files = sorted(DATA.glob('*.zip'))
    if not files:
        raise ValueError('No later archives; run the public-data downloader first')
    with ThreadPoolExecutor(max_workers=4) as pool:
        frames = list(pool.map(read_file, files))
    combined = pd.concat(frames, ignore_index=True)
    groups = {d: g for d, g in combined.groupby('TradDt')}
    start, end = pd.Timestamp(rule['validation_start']), pd.Timestamp(rule['validation_end'])
    first_monday = start - pd.Timedelta(days=start.weekday())
    records = []
    for monday in pd.date_range(first_monday, end, freq='7D'):
        # The partial starting week belongs to discovery, not a new Wednesday signal.
        if monday < start:
            continue
        signal_day = next((d for d in [monday, monday + pd.Timedelta(days=1)] if d in groups), None)
        row = {'week': str(monday.date()), 'signal_date': str(signal_day.date()) if signal_day is not None else None}
        if signal_day is None:
            row['status'] = 'missing_signal_day'
            records.append(row)
            continue
        row['monday_absent'] = signal_day != monday
        row.update(signal_for_day(groups[signal_day], signal_day))
        if row['status'] != 'signal':
            records.append(row)
            continue
        entry_day = signal_day + pd.Timedelta(days=1)
        row['entry_date'] = str(entry_day.date())
        if entry_day not in groups:
            row['status'] = 'missing_entry_day'
            records.append(row)
            continue
        expiry = pd.Timestamp(row['expiry'])
        ec = groups[entry_day]
        ec = ec[ec.XpryDt.eq(expiry) & ec.OptnTp.eq('CE')]
        short, long = ec[ec.StrkPric.eq(row['short'])], ec[ec.StrkPric.eq(row['long'])]
        if len(short) != 1 or len(long) != 1:
            row['status'] = 'missing_or_duplicate_entry_contract'
            records.append(row)
            continue
        s, l = short.iloc[0], long.iloc[0]
        if min(s.ClsPric, l.ClsPric) <= 0 or min(s.TtlNbOfTxsExctd, l.TtlNbOfTxsExctd) < 20:
            row['status'] = 'entry_rejected_liquidity'
            records.append(row)
            continue
        qty, credit = float(s.NewBrdLotQty), float(s.ClsPric-l.ClsPric)
        if qty <= 0 or qty != l.NewBrdLotQty or not 0 < credit < 200:
            row['status'] = 'entry_rejected_credit_or_lot'
            records.append(row)
            continue
        row.update(lot_qty=qty, credit=credit, short_close=float(s.ClsPric), long_close=float(l.ClsPric))
        row['quote_audit'] = quote_audit(row)
        if expiry not in groups:
            row['status'] = 'unexpired_at_cutoff' if expiry > end else 'missing_expiry_data'
            records.append(row)
            continue
        spots = groups[expiry].UndrlygPric
        spots = spots[spots > 0]
        if spots.empty:
            row['status'] = 'missing_expiry_underlying'
            records.append(row)
            continue
        terminal = float(spots.median())
        si, li = max(terminal-row['short'], 0), max(terminal-row['long'], 0)
        gross = credit - (si-li)
        legacy, settlement = fee_points(s.ClsPric, l.ClsPric, si, li, qty)
        row.update(status='closed_proxy', terminal_spot=terminal, debit=si-li, gross_points=gross,
                   discovery_fee_points=legacy, settlement_fee_points=settlement,
                   net_discovery_stress_2pt=gross-legacy-2, net_discovery_stress_5pt=gross-legacy-5,
                   net_settlement_2pt=gross-settlement-2, net_settlement_5pt=gross-settlement-5)
        records.append(row)
    closed = [r for r in records if r['status']=='closed_proxy']
    values = pd.Series([r['net_discovery_stress_2pt'] for r in closed], dtype=float)
    summary = {'rule_sha256': digest, 'archives': len(files), 'data_dates': len(groups),
               'weeks': len(records), 'status_counts': dict(pd.Series([r['status'] for r in records]).value_counts()),
               'closed_proxy_trades': len(closed), 'wins': int((values>0).sum()),
               'mean_net_discovery_stress_2pt': float(values.mean()) if len(values) else None,
               'sum_net_discovery_stress_2pt': float(values.sum()) if len(values) else None,
               'worst_net_points': float(values.min()) if len(values) else None,
               'mean_net_discovery_stress_5pt': sum(r['net_discovery_stress_5pt'] for r in closed)/len(closed) if closed else None,
               'mean_net_settlement_2pt': sum(r['net_settlement_2pt'] for r in closed)/len(closed) if closed else None,
               'execution_validated_closed_trades': 0,
               'execution_note': 'Entry quote feasibility only; no actual fill or full lifecycle execution validation.'}
    OUT.mkdir(exist_ok=True)
    (OUT/'weekly_results.json').write_text(json.dumps(records, indent=2, default=str))
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2, default=int))
    manifest = DATA/'download_manifest.json'
    if manifest.exists():
        (OUT/'download_manifest.json').write_bytes(manifest.read_bytes())
    print(json.dumps(summary, indent=2, default=int))


if __name__ == '__main__':
    run()
