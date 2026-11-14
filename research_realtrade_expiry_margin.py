"""
Strategy 6 at the paper bot's sizing with expiry-day margin at Dhan's real level (2026-11-14).

research_realtrade_real_margin.py books Dhan margin at 11.6% of notional (+2% ELM on expiry day from 2024-11-20).
Live, Dhan asked ~17% on expiry day (2026-10-27: Rs 2.52L per E0 lot at Nifty ~22,740). On paper at Rs 5L, E0 was
then skipped on 2 of the first 3 expiry days (2026-10-27, 2026-11-03) because a 2-lot S2 was still open, while the
replay reported 1 skip in 135 weeks. This counts E0 trades per year with expiry-day entries booked at EXP_PCT.

Sizing is the bot's ("oldbot" in research_realtrade_real_margin.py): model lots, cut to fit 90% of equity at Dhan margin.
Usage:  python research_realtrade_expiry_margin.py [start date, default 2021-01-01]
  A replay from 2021 compounds far above its starting capital by 2024, so margin stops binding there; start in 2024
  or 2025 to see an account that holds that capital at today's index level.
Writes: data/reports/realtrade_expiry_margin_<start>.csv (summary) and ..._e0_<start>.csv (E0 trades by year)
"""
import inspect
import sys
import textwrap

import pandas as pd

import research_realtrade as rt
from research_realtrade_portfolio import sleeves

REAL_PCT = 0.116      # Dhan, non-expiry day
EXP_PCT = 0.17        # Dhan, expiry day (2026-10-27)

_src = textwrap.dedent(inspect.getsource(rt.Weekly.enter))
_old = """    if self.mode != "fixed":
        lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // cap))
"""
_new = """    if self.mode != "fixed":
        if getattr(self, "sizing", "model") != "model" and not longs and lots >= 1:
            pct = (EXP_PCT if self.exp_real and acct.date == exp else REAL_PCT + mpct - MARGIN_PCT)
            dhan = pct * S0 * lot
            lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // dhan))
            cap = dhan
        else:
            lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // cap))
"""
assert _src.count(_old) == 1, "Weekly.enter changed; update the patch"
_ns = dict(vars(rt), REAL_PCT=REAL_PCT, EXP_PCT=EXP_PCT)
exec(_src.replace(_old, _new), _ns)
rt.Weekly.enter = _ns["enter"]


def book(sizing, exp_real, w=(0.55, 0.55, 0.05)):
    out = sleeves(*w)
    for s in out:
        s.sizing, s.exp_real = sizing, exp_real
    return out


if __name__ == "__main__":
    d0 = sys.argv[1] if len(sys.argv) > 1 else "2021-01-01"
    cases = []
    for start in (500_000, 700_000, 1_000_000):
        cases.append((f"{start / 1e5:g}L model 9%", book("model", False), start, True))
        cases.append((f"{start / 1e5:g}L bot, expiry 13.6%", book("oldbot", False), start, True))
        cases.append((f"{start / 1e5:g}L bot, expiry 17%", book("oldbot", True), start, True))
    res = rt.replay_many(cases, d0=d0)
    rows, e0 = [], {}
    for (nm, _, start, _), (E, T, acct) in zip(cases, res.values()):
        if d0 <= rt.IS_END:
            r = rt.report(nm, E, T, acct, start)
            rt.show(r)
        else:                                       # no in-sample years to split off
            r = dict(name=nm, start=start, **rt.metrics(E, T, start))
            print(f"{nm:24s} CAGR {r['cagr']:5.1f} DD {r['max_dd']:6.1f} PF {r['pf_trades']:.2f}")
        rows.append(r)
        t = T[T.sleeve == "E0"]
        e0[nm] = t.groupby(t.entry.astype(str).str[:4]).size()
        e0[nm]["end equity L"] = round(E.equity.iloc[-1] / 1e5, 1)
    pd.DataFrame(rows).to_csv(f"data/reports/realtrade_expiry_margin_{d0[:4]}.csv", index=False)
    tab = pd.DataFrame(e0).fillna(0)
    eq = tab.loc["end equity L"]
    tab = tab.drop("end equity L").astype(int)
    tab.loc["total"] = tab.sum()
    tab.loc["end equity L"] = eq
    tab.to_csv(f"data/reports/realtrade_expiry_margin_e0_{d0[:4]}.csv")
    print("\nE0 trades by entry year\n" + tab.T.to_string())
