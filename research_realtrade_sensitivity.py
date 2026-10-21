"""
Threshold sensitivity of Strategy 6 (final_realtrade_strategy.py): the VIX cut-offs of the short sleeves
(vix_hi) and of the gap buy (vix_lo), base weights 0.45 / 0.45 / 0.05, Rs 10L, running costs.
Writes data/reports/realtrade_sensitivity.csv
"""
import pandas as pd
import research_realtrade as rt
from research_realtrade_portfolio import sleeves

if __name__ == "__main__":
    cfg = []
    for hi in (13, 14, 15, 16, 17):
        for lo in (12, 13, 14, 15):
            cfg.append((f"vix_hi {hi} vix_lo {lo}", sleeves(0.45, 0.45, 0.05, vix_hi=hi, vix_lo=lo), 1_000_000, True))
    rows = []
    for k in range(0, len(cfg), 10):
        res = rt.replay_many(cfg[k:k + 10])
        for nm, (E, T, a) in res.items():
            r = rt.report(nm, E, T, a, 1_000_000)
            rt.show(r)
            rows.append(r)
    pd.DataFrame(rows).to_csv("data/reports/realtrade_sensitivity.csv", index=False)
    print("finished")
