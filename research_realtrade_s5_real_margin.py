"""
Strategy 5 (three_sleeve_bot) on the real-market replay at Dhan's real margin (2026-10-23).

research_realtrade_s5_portfolio.py sizes L2 (30%) and the monthly straddle (20%) on the 9% margin model. Dhan asks
~11.6% of notional (docs/STRATEGY_6_REALTRADE.md section 9). Two readings:
  model 9%    lots as the bot sizes them; at Dhan's rate they tie up ~39% + ~26% (+2% ELM on expiry day) + 3% premium,
              under the 90% cap, so these lots are feasible as they stand
  real 11.6%  the weights are shares of Dhan's real margin -> fewer lots
Real prices only (no synthetic fallback), real-only monthly pricing, running costs on.

Usage:  python research_realtrade_s5_real_margin.py
Writes: data/reports/realtrade_s5_real_margin.csv
"""
import pandas as pd

import research_realtrade as rt
from research_realtrade_s5_portfolio import sleeves

if __name__ == "__main__":
    rt.SYNTHETIC_FALLBACK = False
    rows = []
    for pct in (0.09, 0.116):
        rt.MARGIN_PCT = pct
        cfg = [(f"S5 margin {pct:.1%} {s // 100000}L", sleeves("real"), s, True) for s in (1_000_000, 1_500_000, 2_500_000)]
        for (nm, _, start, _), (E, T, a) in zip(cfg, rt.replay_many(cfg).values()):
            T = T.drop(columns=[c for c in ("row", "mtm") if c in T])
            r = rt.report(nm, E, T, a, start)
            rt.show(r)
            rows.append(r)
    rt.MARGIN_PCT = 0.09
    pd.DataFrame(rows).to_csv("data/reports/realtrade_s5_real_margin.csv", index=False)
