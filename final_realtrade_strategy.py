"""
Strategy 6: VIX-regime weekly options portfolio, backtested as a real-market replay
(research_realtrade.py: real traded 1-minute prices only, no parity/model/intrinsic pricing of live
legs, NSE lot size of the date, whole lots, compounding, margin cap, statutory costs, slippage,
Rs 1,990/month running costs, equity marked to market at every session close).

Rules (chosen on 2021-2023 in research_realtrade_grid*.py and research_realtrade_portfolio.py;
2024-01 onward is out-of-sample). "VIX" = previous session's India VIX close.
  S2   VIX >= 15: at 09:30 two sessions before the weekly expiry, sell the strangle at
       ATM +/- 1.25 x ATM straddle price (rounded to 50, max 450 from ATM). Exit when the loss reaches
       1.0 x the credit (1-minute MTM on LTP, market order), else 15:15 on expiry day.
       Size: 45% of equity as margin (9% of notional per lot).
  E0   VIX >= 15: at 09:30 on expiry day, sell the strangle at ATM +/- 0.5 x straddle. Stop at 0.5 x
       credit, else 15:15. Size: 45% of equity as margin (11% per lot on expiry day from 2024-11-20).
  GAP  VIX <= 13: at 09:30 four sessions before expiry, buy the ATM CE if spot is above the previous
       close (PE if below). Resting limit sell at 2 x the fill, else 15:15 on expiry day.
       Size: 5% of equity in premium.
  Total margin is capped at 90% of equity; a sleeve that would exceed it trades fewer lots.

Usage:  python final_realtrade_strategy.py            (base case + stress tests)
Writes: data/reports/realtrade_strategy_summary.json, realtrade_strategy_trades.csv,
        realtrade_strategy_equity.csv
"""
import json
import sys

import pandas as pd

import cost_model
import research_realtrade as rt
from research_realtrade_portfolio import sleeves

W_S2, W_E0, F_GAP = 0.45, 0.45, 0.05
START = 1_000_000


def run(start=START, slip_mult=1.0, margin_pct=0.09, running=True, w=(W_S2, W_E0, F_GAP)):
    base = cost_model.slip
    rt.MARGIN_PCT = margin_pct
    rt.slip = lambda p, urgent=False: slip_mult * base(p, urgent)
    try:
        E, T, acct = rt.replay(sleeves(*w), start, running)
    finally:
        rt.slip, rt.MARGIN_PCT = base, 0.09
    return E, T, acct, rt.report("", E, T, acct, start)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = {}
    cases = {"base 10L": dict()}
    if which == "all":
        cases.update({
            "slippage x2": dict(slip_mult=2.0),
            "slippage x3": dict(slip_mult=3.0),
            "margin 12%": dict(margin_pct=0.12),
            "start 3L": dict(start=300_000),
            "start 5L": dict(start=500_000),
            "start 25L": dict(start=2_500_000),
            "start 50L": dict(start=5_000_000),
            "half size": dict(w=(W_S2 / 2, W_E0 / 2, F_GAP / 2)),
        })
    for name, kw in cases.items():
        E, T, acct, r = run(**kw)
        r["name"] = name
        rt.show(r)
        out[name] = r
        if name == "base 10L":
            T.to_csv("data/reports/realtrade_strategy_trades.csv", index=False)
            E.to_csv("data/reports/realtrade_strategy_equity.csv", index=False)
    if which == "all":
        with open("data/reports/realtrade_strategy_summary.json", "w") as fh:
            json.dump(out, fh, indent=1, default=str)
