"""
Strategy 7: Strategy 6 + the monthly short straddle, for bigger accounts (Rs 10L+), on the real-market replay.
The improved successor of Strategy 5 / three_sleeve_bot (research: research_realtrade_s7.py).

Sleeves ("VIX" = previous session's India VIX close):
  S2   VIX >= 15: sell ATM +/- 1.25 x straddle, 09:30 two sessions before expiry, stop 1.0 x credit. 45% margin.
  E0   VIX >= 15: sell ATM +/- 0.5 x straddle on expiry day 09:30, stop 0.5 x credit. 45% margin.
  GAP  VIX <= 13: buy ATM CE/PE in the 09:30 gap direction 4 sessions before expiry, limit 2x. 5% premium.
  MON  VIX - RV20 >= 2 (previous session): sell the ATM straddle of the monthly expiry at ~45 DTE (~10:15),
       buy back at 21 DTE, no stop. 20% of equity as margin (Strategy 5's weight, not re-tuned).
       P&L on real prices only (research_monthly_realonly.py): an exit at a strike with no traded price uses
       the last real price moved by the change in intrinsic value, plus urgent slippage (conservative).
Dropped from Strategy 5: the L2 staggered strangle (tranche PF ~1.25 on real prices even when VIX-gated).
Margin: 11.6% of notional (+2% on expiry day), Dhan's quote on 2026-10-22; total capped at 90% of equity.
Running costs Rs 590/month (Lambda bot).

Usage: python final_realtrade_s7.py [base]
Writes data/reports/realtrade_s7_summary.json, realtrade_s7_trades.csv, realtrade_s7_equity.csv
"""
import json
import sys

import cost_model
import research_realtrade as rt
from research_realtrade_s7 import MonthlyReal, s6

sys.path.insert(0, "strategy6_bot")
import rules as R  # noqa: E402

START = 1_500_000
W_MON = 0.20


def run(start=START, slip_mult=1.0, margin=0.116, w_mon=W_MON, w=(0.45, 0.45, 0.05)):
    base = cost_model.slip
    rt.MARGIN_PCT, rt.RUNNING = margin, R.MONTHLY_RUNNING_COSTS
    rt.slip = lambda p, urgent=False: slip_mult * base(p, urgent)
    try:
        sl = s6(*w) + ([MonthlyReal(w_mon)] if w_mon else [])
        E, T, acct = rt.replay(sl, start, True)
    finally:
        rt.slip, rt.MARGIN_PCT = base, 0.09
    T = T.drop(columns=[c for c in ("row", "mtm") if c in T])
    return E, T, acct, rt.report("", E, T, acct, start)


if __name__ == "__main__":
    cases = {"base 15L": {}}
    if len(sys.argv) < 2 or sys.argv[1] != "base":
        cases.update({
            "without MON (Strategy 6)": dict(w_mon=0),
            "slippage x2": dict(slip_mult=2.0),
            "margin 14%": dict(margin=0.14),
            "MON weight 0.15": dict(w_mon=0.15),
            "MON weight 0.25": dict(w_mon=0.25),
            "start 10L": dict(start=1_000_000),
            "start 25L": dict(start=2_500_000),
            "start 50L": dict(start=5_000_000),
        })
    out = {}
    for name, kw in cases.items():
        E, T, acct, r = run(**kw)
        r["name"] = name
        rt.show(r)
        out[name] = r
        if name == "base 15L":
            T.to_csv("data/reports/realtrade_s7_trades.csv", index=False)
            E.to_csv("data/reports/realtrade_s7_equity.csv", index=False)
    if len(cases) > 1:
        with open("data/reports/realtrade_s7_summary.json", "w") as fh:
            json.dump(out, fh, indent=1, default=str)
