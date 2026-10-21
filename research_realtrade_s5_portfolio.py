"""
Strategy 5 (three_sleeve_bot) as a whole account on the real-market replay, Rs 15L (the bot's paper
capital) and Rs 10L, running costs on, versus final_portfolio_strategy.py (the research booking).
  L2   bot rules (5-min stop checks, >= 3 lots), 30% margin
  GAP  bot rules (VIX 09:20 <= 15, 5-min close >= 2x fill), 3% premium
  MON  monthly straddle, VRP >= 2, 20% margin, P&L from research_monthly_realonly.py (real prices only,
       `real`) or the research parity/model pricing (`research`); marked daily from its own MTM path.
Writes data/reports/realtrade_s5_portfolio.csv
"""
import sys
import pandas as pd

import research_realtrade as rt
from cost_model import get_lot_size


class Monthly:
    sleeve = "MON"

    def __init__(self, pricing="real", weight=0.20, vrp_min=2.0):
        m = pd.read_pickle("data/reports/monthly_realonly_trades.pkl")
        m = m[m.vrp >= vrp_min]
        self.by_entry = {r.entry: r for r in m.itertuples()}
        self.pricing, self.w = pricing, weight

    def on_minute(self, acct, day, m, cal, dctx):
        if m != 60:                     # research fills on the 09:15 hourly bar close (~10:15)
            return
        for t in [t for t in acct.open if t["sleeve"] == self.sleeve]:
            r = t["meta"]["row"]
            units = t["meta"]["units"]
            if acct.date >= r.exit:
                rs = r.rs_real if self.pricing == "real" else r.rs_research
                pnl = rs / 65 * units
                acct.cash += pnl
                t.update(pnl=pnl, open=False, exit=acct.date, reason="time", vmark=0.0)
                acct.open.remove(t)
            else:
                t["vmark"] = t["meta"]["mtm"].get(acct.date, t.get("vmark", 0.0) / units * 65 if units else 0) * units \
                    if acct.date in t["meta"]["mtm"] else t.get("vmark", 0.0)
        r = self.by_entry.get(acct.date)
        if r is not None:
            lot = get_lot_size(acct.date)
            lots = int(self.w * acct.equity() // (rt.MARGIN_PCT * r.spot * lot))
            if lots:
                t = dict(sleeve=self.sleeve, entry=acct.date, entry_min=m, legs=[], pnl=0.0, costs=0.0, open=True,
                         credit=r.credit, exit=None, reason=None, vmark=0.0,
                         meta=dict(row=r, units=lots * lot, lots=lots, mtm=r.mtm))
                acct.trades.append(t)
                acct.open.append(t)


def sleeves(pricing):
    return [rt.StaggeredStrangle(0.30, stop=1.5, check_every=5, min_total=3),
            rt.GapBuy(0.03, vix_max=15, target="close", vix_src="0920", check_every=5),
            Monthly(pricing)]


if __name__ == "__main__":
    mode = sys.argv[1]                      # synthetic = old engine pricing everywhere, real = real prices only
    rt.SYNTHETIC_FALLBACK = mode == "synthetic"
    pricing = "research" if mode == "synthetic" else "real"
    cfg = [(f"S5 {mode} {s // 100000}L", sleeves(pricing), s, True) for s in (1_000_000, 1_500_000, 2_500_000)]
    rows = []
    for nm, (E, T, a) in rt.replay_many(cfg).items():
        T = T.drop(columns=[c for c in ("row", "mtm") if c in T])
        r = rt.report(nm, E, T, a, cfg[[c[0] for c in cfg].index(nm)][2])
        rt.show(r)
        rows.append(r)
        if "15L" in nm:
            T.to_csv(f"data/reports/realtrade_s5_portfolio_trades_{mode}.csv", index=False)
            E.to_csv(f"data/reports/realtrade_s5_portfolio_equity_{mode}.csv", index=False)
    pd.DataFrame(rows).to_csv(f"data/reports/realtrade_s5_portfolio_{mode}.csv", index=False)
    print("finished", flush=True)
