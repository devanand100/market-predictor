"""
Sizing search for the real-price portfolio (research_realtrade.py), one account per combination,
Rs 10L start, running costs on. Sleeves (rules fixed from the IS grids, research_realtrade_grid3.py):
  S2  short strangle at ATM +/- 1.25 x straddle, 09:30 two sessions before expiry, stop 1.0 x credit,
      only when the previous session's VIX close >= 15; else 15:15 expiry day
  E0  expiry-day short strangle at ATM +/- 0.5 x straddle, 09:30, stop 0.5 x credit, previous VIX >= 15
  GAP ATM option buy in the 09:30 gap direction 4 sessions before expiry, limit target +100%,
      previous VIX <= 13
Weights: S2/E0 = fraction of equity used as margin, GAP = fraction of equity paid as premium.
Selection on IS (2021-23); writes data/reports/realtrade_portfolio_grid.csv
"""
import itertools
import sys
import pandas as pd

import research_realtrade as rt
from research_realtrade_grid import strangle, long_dir

W = rt.Weekly


def sleeves(w_s2, w_e0, f_gap, vix_hi=15, vix_lo=13):
    out = []
    if w_s2:
        out.append(W("S2", strangle(1.25), dte=2, stop=1.0, vix_min=vix_hi, mode="risk", weight=w_s2))
    if w_e0:
        out.append(W("E0", strangle(0.5), dte=0, stop=0.5, vix_min=vix_hi, mode="risk", weight=w_e0))
    if f_gap:
        out.append(W("GAP", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=vix_lo,
                     mode="risk", weight=f_gap))
    return out


def grid():
    return list(itertools.product([0.3, 0.45, 0.6], [0.3, 0.45, 0.6], [0.0, 0.03, 0.05, 0.08]))


if __name__ == "__main__":
    part, nparts = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (0, 1)
    G = grid()[part::nparts]
    start = 1_000_000
    res = rt.replay_many([(f"S2 {a} E0 {b} GAP {c}", sleeves(a, b, c), start, True) for a, b, c in G])
    rows = []
    for nm, (E, T, acct) in res.items():
        r = rt.report(nm, E, T, acct, start)
        rt.show(r)
        rows.append(r)
    pd.DataFrame(rows).to_csv(f"data/reports/realtrade_portfolio_grid_{part}.csv", index=False)
    print(part, "finished", flush=True)
