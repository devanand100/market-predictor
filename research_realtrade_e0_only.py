"""
E0 (expiry-day strangle) alone vs full Strategy 6 on the real-price replay, 2021 to 2026-10-01 (2026-11-14).
Weight 0.55, Rs 5L and Rs 10L, 9% margin model and the bot sizing at Dhan 17% on expiry day
(patch from research_realtrade_expiry_margin.py). Prints CAGR / DD / PF, IS 2021-23 vs 2024+, and E0 trades by year.
Usage:  python research_realtrade_e0_only.py   (results in docs/STRATEGY_6_REALTRADE.md section 11)
"""
import sys
import pandas as pd
import research_realtrade as rt
import research_realtrade_expiry_margin as em          # applies the Dhan-margin patch (main is guarded)
from research_realtrade_portfolio import sleeves

def bk(w, sizing, exp_real):
    out = sleeves(*w)
    for s in out: s.sizing, s.exp_real = sizing, exp_real
    return out

cases = []
for start in (500_000, 1_000_000):
    L = f"{start/1e5:g}L"
    cases += [(f"{L} E0 only, 9% model", bk((0, 0.55, 0), "model", False), start, True),
              (f"{L} E0 only, Dhan 17%", bk((0, 0.55, 0), "oldbot", True), start, True),
              (f"{L} Strategy 6, Dhan 17%", bk((0.55, 0.55, 0.05), "oldbot", True), start, True)]
res = rt.replay_many(cases)
for (nm, _, start, _), (E, T, acct) in zip(cases, res.values()):
    r = rt.report(nm, E, T, acct, start)
    t = T[T.sleeve == "E0"]
    wins = (t.pnl > 0).mean() * 100
    print(f"{nm:28s} CAGR {r['cagr']:5.1f} DD {r['max_dd']:6.1f} PF {r['pf_trades']:.2f} trades {len(t)} win% {wins:.0f} "
          f"| 2021-23 {r['is_cagr']:5.1f}/{r['is_dd']:6.1f} | 2024+ {r['oos_cagr']:5.1f}/{r['oos_dd']:6.1f} | years {r['years']}")
    if "only" in nm:
        y = t.groupby(t.entry.astype(str).str[:4]).agg(n=("pnl", "size"), win=("pnl", lambda p: round((p > 0).mean() * 100)),
                                                       net=("pnl", lambda p: round(p.sum())), worst=("pnl", lambda p: round(p.min())))
        print(y.T.to_string())
