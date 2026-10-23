"""
Strategy 6 replay with the paper bot's Dhan-margin sizing (strategy6_bot margin_gate, 2026-10-23).

The final backtest sizes every naked strangle at the 9% margin model. The bot quotes Dhan's calculator first; Dhan
asks ~11.6% of notional (2026-10-22/23: Rs 1.70-1.72L per lot at Nifty ~22,700, lot 65), +2% ELM on expiry day.
Modes per strangle sleeve:
  model   backtest as published: lots = w x eq / model, capped by 90% of equity; margin booked at the model
  oldbot  bot until 2026-10-23: model lots, cut only to fit 90% of equity at Dhan margin; Dhan margin booked
  newbot  bot from 2026-10-23: lots = min(model lots, max(1, w x eq / Dhan per lot), 90% room / Dhan per lot)

Usage:  python research_realtrade_real_margin.py [capital]
Writes: data/reports/realtrade_real_margin.csv (capital sweep: realtrade_real_margin_capital.csv)
"""
import inspect
import sys
import textwrap

import pandas as pd

import research_realtrade as rt
from research_realtrade_portfolio import sleeves

REAL_PCT = 0.116

_src = textwrap.dedent(inspect.getsource(rt.Weekly.enter))
_old = """    if self.mode != "fixed":
        lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // cap))
"""
_new = """    if self.mode != "fixed":
        sizing = getattr(self, "sizing", "model")
        if sizing != "model" and not longs and lots >= 1:
            dhan = (REAL_PCT + mpct - MARGIN_PCT) * S0 * lot
            room = int((MAX_UTIL * eq - acct.margin_used()) // dhan)
            lots = min(lots, room) if sizing == "oldbot" else min(lots, max(1, int(self.w * eq // dhan)), room)
            cap = dhan
        else:
            lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // cap))
"""
assert _src.count(_old) == 1, "Weekly.enter changed; update the patch"
_src = _src.replace(_old, _new)
_ns = dict(vars(rt), REAL_PCT=REAL_PCT)
exec(_src, _ns)
rt.Weekly.enter = _ns["enter"]


def book(w, sizing):
    out = sleeves(*w)
    for s in out:
        s.sizing = sizing
    return out


if __name__ == "__main__":
    cases = []
    if len(sys.argv) > 1 and sys.argv[1] == "capital":         # bot weights 0.55 / 0.55 / 0.05 at several capitals
        for start in (300_000, 400_000, 500_000, 750_000, 1_000_000, 1_500_000, 2_000_000):
            for sizing in ("oldbot", "newbot"):
                cases.append((f"{start / 100_000:g}L {sizing}", book((0.55, 0.55, 0.05), sizing), start, True))
    else:
        for start, w in ((500_000, (0.55, 0.55, 0.05)), (1_000_000, (0.45, 0.45, 0.05))):
            for sizing in ("model", "oldbot", "newbot"):
                cases.append((f"{start // 100_000}L {sizing}", book(w, sizing), start, True))
    res = rt.replay_many(cases)
    rows = []
    for (nm, _, start, _), (E, T, acct) in zip(cases, res.values()):
        r = rt.report(nm, E, T, acct, start)
        rt.show(r)
        rows.append(r)
    pd.DataFrame(rows).to_csv("data/reports/realtrade_real_margin" + ("_capital" if len(sys.argv) > 1 else "") + ".csv",
                              index=False)
