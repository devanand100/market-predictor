"""Stress tests for the delta-hedged strangle (D2): futures slippage, basis drift, IV source, size."""
import pandas as pd
import cost_model
import research_institutional as ri
import research_positional as rp
from engine import load_days, load_daily

days, daily = load_days(), load_daily()
cyc = rp.cycles(daily, days)
cases = [("base: slip 1pt, band 1 lot, 10 lots", dict(), 1, 10),
         ("futures slippage 2 pts", dict(FUT_SLIP_PTS=2.0), 1, 10),
         ("futures slippage 3 pts", dict(FUT_SLIP_PTS=3.0), 1, 10),
         ("basis drift 5 pts/day vs hedge", dict(BASIS=5.0), 1, 10),
         ("ATM IV only for deltas", dict(ATM=True), 1, 10),
         ("all three: slip 2 + basis 5 + ATM IV", dict(FUT_SLIP_PTS=2.0, BASIS=5.0, ATM=True), 1, 10),
         ("band 2 lots, slip 2 + basis 5", dict(FUT_SLIP_PTS=2.0, BASIS=5.0), 2, 10),
         ("4 lots (Rs ~10L), band 1 lot", dict(), 1, 4),
         ("4 lots, band 1 lot, slip 2 + basis 5", dict(FUT_SLIP_PTS=2.0, BASIS=5.0), 1, 4)]
rows = []
for name, cfg, band, lots in cases:
    cost_model.FUT_SLIP_PTS = cfg.get("FUT_SLIP_PTS", 1.0)
    ri.BASIS_PTS_PER_DAY = cfg.get("BASIS", 0.0)
    ri.ATM_IV_ONLY = cfg.get("ATM", False)
    T = pd.DataFrame([r for w, e in cyc if (r := ri.run(days, w, "D2", lots=lots, band_lots=band))])
    rows.append(ri.summarize(T, name))
    print(rows[-1], flush=True)
pd.DataFrame(rows).to_csv("data/reports/research_institutional_stress.csv", index=False)
