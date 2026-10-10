#!/usr/bin/env python3
"""tools/margin_sizing.py — how many IC lots the capital stack can carry under
a joint stress, with and without a T-bill / G-sec cash-equivalent layer.

Pure arithmetic, no broker calls: pass the numbers from `get_limits` (or the
"LIVE margin:" log line) on the command line.

Constraints that must all hold at the stressed margin (lots * per_lot * (1+s_m)),
after paying ONE side's gap loss (lots * exit_loss_per_lot) out of cash — the
buy-back of a breached spread is settled in real cash, T-bills/equity cannot pay it:
  1. capacity : cash + equity*(1-s_e) + tbill*(1-h)*(1-s_t) - loss >= margin
  2. 50% rule : cash + tbill*(1-h)*(1-s_t) - loss                  >= 0.5*margin
  3. cash     : cash - loss                                        >= 0
     (equity / SGB collateral does NOT count toward the cash half)

Example:
  python3 tools/margin_sizing.py --cash 627000 --collateral 753000
"""

import argparse
import math


def max_lots(cash, equity, tbill_mkt, per_lot, s_e, s_m, s_t, haircut, cash_buffer=0.0, loss_per_lot=0.0):
    tbill = tbill_mkt * (1 - haircut) * (1 - s_t)
    stressed_lot = per_lot * (1 + s_m)
    capacity = (cash - cash_buffer) + equity * (1 - s_e) + tbill
    cash_half = cash + tbill
    by_capacity = math.floor(capacity / (stressed_lot + loss_per_lot))
    by_rule = math.floor(cash_half / (0.5 * stressed_lot + loss_per_lot))
    by_cash = math.floor(cash / loss_per_lot) if loss_per_lot > 0 else by_capacity
    return min(by_capacity, by_rule, by_cash), by_capacity, by_rule


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cash", type=float, required=True, help="broker cash (Rs)")
    ap.add_argument("--collateral", type=float, required=True, help="equity collateral after haircut (Rs)")
    ap.add_argument("--per-lot", type=float, default=85677.0, help="peak margin per lot (default: 1,113,803/13)")
    ap.add_argument("--equity-stress", type=float, default=0.25)
    ap.add_argument("--margin-stress", type=float, default=0.25)
    ap.add_argument("--tbill-stress", type=float, default=0.01)
    ap.add_argument("--haircut", type=float, default=0.05, help="T-bill/G-sec haircut (default 5%%)")
    ap.add_argument("--yield", dest="yld", type=float, default=0.063, help="T-bill yield (default 6.3%%)")
    ap.add_argument("--cash-buffer", type=float, default=0.0, help="cash kept out of the margin pool")
    ap.add_argument("--exit-loss-per-lot", type=float, default=14000.0,
                    help="cash loss per lot if ONE spread is breached and bought back in a gap "
                         "(default ~(250pt wing*65) - ~2.3k credit; set 0 to disable)")
    ap.add_argument("--equity-haircut", type=float, default=0.20,
                    help="haircut on the pledged equity, to back out its market value (default 20%%)")
    ap.add_argument("--max-move", type=float, default=None, help="largest cash->T-bill move to scan (Rs)")
    ap.add_argument("--step", type=float, default=50000.0)
    a = ap.parse_args()

    move_cap = a.max_move if a.max_move is not None else max(a.cash - 150000.0, 0.0)
    scenarios = (
        ("moderate", min(0.15, a.equity_stress), min(0.15, a.margin_stress)),
        ("severe", a.equity_stress, a.margin_stress),
    )
    print(f"cash={a.cash:,.0f} equity_collateral={a.collateral:,.0f} per_lot={a.per_lot:,.0f} "
          f"haircut={a.haircut:.0%} yield={a.yld:.1%} (assumed) exit_loss/lot={a.exit_loss_per_lot:,.0f}\n")
    print(f"{'moved to T-bills':>16} {'cash left':>10} {'yield/yr':>9} | " +
          " | ".join(f"{n} lots (cap/rule)" for n, _, _ in scenarios))
    x = 0.0
    while x <= move_cap + 1e-6:
        cells = []
        for _, s_e, s_m in scenarios:
            lots, cap, rule = max_lots(a.cash - x, a.collateral, x, a.per_lot, s_e, s_m,
                                       a.tbill_stress, a.haircut, a.cash_buffer, a.exit_loss_per_lot)
            cells.append(f"{lots:>3} ({cap}/{rule})".ljust(20))
        print(f"{x:>16,.0f} {a.cash - x:>10,.0f} {x * a.yld:>9,.0f} | " + " | ".join(cells))
        x += a.step

    # Swap pledged equity (sold, realising tax) into T-bills/G-secs: the
    # market value goes into the cash-equivalent layer at the lower haircut.
    mkt = a.collateral / (1 - a.equity_haircut)
    print(f"\nSWAP pledged equity -> T-bills/G-secs (equity market value ~{mkt:,.0f}; "
          f"before tax and without the equity's own return)")
    print(f"{'equity sold':>12} {'collateral left':>16} | " +
          " | ".join(f"{n} lots (cap/rule)" for n, _, _ in scenarios))
    frac = 0.0
    while frac <= 1.0 + 1e-9:
        sold_mkt = mkt * frac
        left_eq = a.collateral * (1 - frac)
        cells = []
        for _, s_e, s_m in scenarios:
            lots, cap, rule = max_lots(a.cash, left_eq, sold_mkt, a.per_lot, s_e, s_m,
                                       a.tbill_stress, a.haircut, a.cash_buffer, a.exit_loss_per_lot)
            cells.append(f"{lots:>3} ({cap}/{rule})".ljust(20))
        print(f"{sold_mkt:>12,.0f} {left_eq:>16,.0f} | " + " | ".join(cells))
        frac += 0.25


if __name__ == "__main__":
    main()
