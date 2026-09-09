"""Financial and temporal invariants for offline research; never invokes a broker."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('pcr_validation', Path(__file__).parents[1]/'tools/validate_pcr_edge.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_pcr_threshold_is_strict_and_chain_includes_far_strikes():
    d = pd.Timestamp('2026-06-01')
    f = pd.DataFrame({'OptnTp': ['CE','PE','PE'], 'XpryDt': [d+pd.Timedelta(days=8)]*3,
                      'StrkPric': [24000,24000,20000], 'OpnIntrst': [100,60,10], 'UndrlygPric': [24000]*3})
    assert module.signal_for_day(f,d)['status']=='no_signal'
    f.loc[2,'OpnIntrst']=9
    s=module.signal_for_day(f,d)
    assert s['status']=='signal'
    assert (s['short'],s['long'])==(24200,24400)


def test_expiry_selection_skips_one_day_and_uses_eligible_chain_only():
    d=pd.Timestamp('2026-06-01')
    f=pd.DataFrame({'OptnTp':['CE','PE','CE','PE'], 'XpryDt':[d+pd.Timedelta(days=1)]*2+[d+pd.Timedelta(days=8)]*2,
                    'StrkPric':[24000]*4, 'OpnIntrst':[100,200,100,50], 'UndrlygPric':[24000]*4})
    assert module.signal_for_day(f,d)['pcr']==.5


def test_settlement_fees_do_not_charge_synthetic_exit_and_use_current_exercise_rate():
    _,otm=module.fee_points(40,10,0,0,65)
    _,itm=module.fee_points(40,10,300,100,65)
    assert itm-otm==pytest.approx(100*.0015)
    with pytest.raises(ValueError):module.fee_points(40,10,0,0,0)


def test_quote_requires_later_fresh_price_and_sufficient_depth():
    target=pd.Timestamp('2026-06-02T09:50:01Z')
    rows=[]
    for seconds,age,qty in [(-1,0,65),(1,20,65),(2,0,64),(3,0,65)]:
        t=target+pd.Timedelta(seconds=seconds)
        rows.append(dict(snap_time=t.isoformat(),feed_time=t.timestamp()-age,bid=10,ask=11,bid_qty=qty,ask_qty=qty))
    q=module.choose_quote(pd.DataFrame(rows),target,'BUY',65)
    assert pd.Timestamp(q['time'])==target+pd.Timedelta(seconds=3)
    assert q['price']==11
    assert module.choose_quote(pd.DataFrame(rows).drop(columns='feed_time'),target,'BUY',65) is None


def test_locked_rule_digest_matches():
    rule,digest=module.locked_rule()
    assert rule['pcr_strictly_below']==.7
    assert len(digest)==64


@pytest.mark.parametrize('include_expiry, expected', [(True,'closed_proxy'),(False,'missing_expiry_data')])
def test_run_preserves_missing_outcomes_and_uses_later_prices(tmp_path, monkeypatch, include_expiry, expected):
    signal=pd.Timestamp('2026-05-25')
    entry=pd.Timestamp('2026-05-26')
    expiry=pd.Timestamp('2026-06-02')
    rows=[]
    for day,spot in [(signal,24000),(entry,24000)]+([(expiry,24500)] if include_expiry else []):
        for typ,strike,oi,price in [('CE',24200,100,40),('CE',24400,100,10),('PE',24000,100,20)]:
            rows.append(dict(TradDt=day,XpryDt=expiry,OptnTp=typ,StrkPric=strike,
                             OpnIntrst=oi,UndrlygPric=spot,ClsPric=price,TtlNbOfTxsExctd=50,NewBrdLotQty=65))
    (tmp_path/'fake.zip').touch()
    monkeypatch.setattr(module,'DATA',tmp_path)
    monkeypatch.setattr(module,'OUT',tmp_path/'results')
    monkeypatch.setattr(module,'read_file',lambda _:pd.DataFrame(rows))
    monkeypatch.setattr(module,'quote_audit',lambda _: {'status':'missing_recorded_tick_contract'})
    module.run()
    import json
    result=json.loads((tmp_path/'results/weekly_results.json').read_text())[0]
    assert result['status']==expected
    assert result['entry_date']=='2026-05-26'
    if include_expiry:
        assert result['debit']==200
        assert result['gross_points']==-170
        assert result['net_discovery_stress_2pt'] < -172
    else:
        assert 'gross_points' not in result
