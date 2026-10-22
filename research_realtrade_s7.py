"""
Strategy 7 research: an improved three-sleeve portfolio (three_sleeve_bot / Strategy 5) for bigger capital, on the
real-market replay (research_realtrade.py: real traded prices only, lot size by date, costs, running costs).

Stage 1 (`python research_realtrade_s7.py l2`): L2 staggered-strangle variants, sizing-free (Rs 10 crore account,
  no running costs): VIX gate on the previous session's close, width multiple, tranche stop.
Stage 2 (`python research_realtrade_s7.py port`): portfolios at Rs 15L and Rs 25L with Dhan-level margin
  (11.6% of notional, +2% on expiry day; Dhan quoted Rs 1.72L per strangle lot on 2026-10-22), Rs 590/month:
  Strategy 5 as the bot ran it, Strategy 6, and Strategy 6 plus a gated L2 and/or the monthly straddle
  (VRP >= 2, priced on real prices only by research_monthly_realonly.py, conservative for unpriced exits).
Selection on 2021-2023 (IS); 2024+ is out-of-sample.
Writes data/reports/realtrade_s7_l2.csv, realtrade_s7_port.csv
"""
import sys

import pandas as pd

import research_realtrade as rt
from cost_model import get_lot_size
from research_realtrade_grid import strangle, long_dir

sys.path.insert(0, "strategy6_bot")
import rules as R  # noqa: E402

W = rt.Weekly
DHAN_MARGIN = 0.116


class L2Gated:
    """L2 staggered strangle (bot rules: 1/3 of the size on each of the first 3 sessions at 09:30, strikes at
    ATM +/- mult x straddle, each tranche stops at stop x its credit, else 15:15 expiry), traded only when the
    previous session's VIX close (on the cycle's first session) is inside [vix_min, vix_max]. Each tranche
    books its margin and is cut to fit the account's MAX_UTIL cap."""

    def __init__(self, weight=0.30, stop=1.5, mult=1.0, tranches=3, min_total=3, check_every=1,
                 vix_min=None, vix_max=None, name="L2"):
        self.w, self.stop, self.mult, self.n, self.min_total, self.k = weight, stop, mult, tranches, min_total, check_every
        self.vix_min, self.vix_max, self.sleeve = vix_min, vix_max, name
        self.lots = 0

    def on_minute(self, acct, day, m, cal, dctx):
        i, n, exp = cal
        if m == rt.ENTRY_MIN and i < self.n and i < n - 1:
            lot, spot = get_lot_size(acct.date), day.spot[m]
            if i == 0:
                v = dctx["vix"]
                gate = not ((self.vix_min and v < self.vix_min) or (self.vix_max and v > self.vix_max))
                total = int(self.w * acct.equity() // (rt.MARGIN_PCT * spot * lot))
                self.lots = total // self.n if gate and total >= self.min_total else 0
            cap = rt.margin_pct(acct.date, acct.date == exp) * spot * lot
            lots = min(self.lots, int((rt.MAX_UTIL * acct.equity() - acct.margin_used()) // cap)) if self.lots else 0
            if lots > 0:
                K = rt.atm_strike(spot)
                ce, pe = rt.real(day, K, "CE", m), rt.real(day, K, "PE", m)
                if ce is not None and pe is not None:
                    w = min(rt.WINDOW_MAX, max(50, int(round(self.mult * (ce + pe) / 50)) * 50))
                    q = lots * lot
                    acct.open_group(day, m, self.sleeve, [("CE", K + w, -q), ("PE", K - w, -q)],
                                    dict(width=w, lots=lots, spot=spot, exp=exp, margin=cap * lots))
        for t in [t for t in acct.open if t["sleeve"] == self.sleeve]:
            if acct.date == t["meta"]["exp"] and m >= rt.EXIT_MIN:
                acct.close_group(t, day, m, "expiry")
                continue
            if (t["entry"] == acct.date and m <= t["entry_min"]) or m % self.k:
                continue
            if acct.group_mtm(t) <= -self.stop * t["credit"]:
                acct.close_group(t, day, m, "stop", urgent=True)


class MonthlyReal:
    """Monthly 45->21 DTE short straddle (VRP >= 2) with P&L from real prices only (research_monthly_realonly.py),
    marked daily from its own MTM path, margin booked against the account cap."""
    sleeve = "MON"

    def __init__(self, weight=0.20, vrp_min=2.0):
        mm = pd.read_pickle("data/reports/monthly_realonly_trades.pkl")
        self.by_entry = {r.entry: r for r in mm[mm.vrp >= vrp_min].itertuples()}
        self.w = weight

    def on_minute(self, acct, day, m, cal, dctx):
        if m != 60:
            return
        for t in [t for t in acct.open if t["sleeve"] == self.sleeve]:
            r, units = t["meta"]["row"], t["meta"]["units"]
            if acct.date >= r.exit:
                pnl = r.rs_real / 65 * units
                acct.cash += pnl
                t.update(pnl=pnl, open=False, exit=acct.date, reason="time", vmark=0.0)
                acct.open.remove(t)
            elif acct.date in r.mtm:
                t["vmark"] = r.mtm[acct.date] * units
        r = self.by_entry.get(acct.date)
        if r is not None:
            lot = get_lot_size(acct.date)
            cap = rt.MARGIN_PCT * r.spot * lot
            lots = min(int(self.w * acct.equity() // cap), int((rt.MAX_UTIL * acct.equity() - acct.margin_used()) // cap))
            if lots > 0:
                t = dict(sleeve=self.sleeve, entry=acct.date, entry_min=m, legs=[], pnl=0.0, costs=0.0, open=True,
                         credit=r.credit, exit=None, reason=None, vmark=0.0,
                         meta=dict(row=r, units=lots * lot, lots=lots, margin=cap * lots, spot=r.spot))
                acct.trades.append(t)
                acct.open.append(t)


def s6(w2=0.45, we=0.45, fg=0.05):
    return [W("S2", strangle(1.25), dte=2, stop=1.0, vix_min=15, mode="risk", weight=w2),
            W("E0", strangle(0.5), dte=0, stop=0.5, vix_min=15, mode="risk", weight=we),
            W("GAP", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=13, mode="risk", weight=fg)]


def week_pf(T, info):
    T = T[T.sleeve != "running"].copy()
    T["lsz"] = T.entry.map(get_lot_size)
    T["pu"] = T.pnl / (T.lots * T.lsz)
    T["wk"] = T.entry.map(lambda d: info[d][2])
    w = T.groupby("wk").pu.sum()
    i, o = w[w.index <= rt.IS_END], w[w.index > rt.IS_END]
    return dict(weeks=len(w), pf=rt.pf(w), is_pf=rt.pf(i), oos_pf=rt.pf(o), is_pts=round(i.mean(), 1),
                oos_pts=round(o.mean(), 1), **{y: round(g.sum()) for y, g in w.groupby(w.index.str[:4])})


def stage_l2():
    days, daily = rt.data()
    info, _ = rt.calendar(days, daily)
    V = []
    for vmin, vmax in ((None, None), (13, None), (15, None), (17, None), (13, 15)):
        V.append((f"L2 vix {vmin}-{vmax}", dict(vix_min=vmin, vix_max=vmax)))
    for mult in (1.25,):
        for stop in (1.0, 1.5):
            V.append((f"L2 vix 15- mult {mult} stop {stop}", dict(vix_min=15, mult=mult, stop=stop)))
    V.append(("L2 vix 15- stop 1.0", dict(vix_min=15, stop=1.0)))
    V.append(("L2 vix 15- 5-min checks", dict(vix_min=15, check_every=5)))
    cfg = []
    for nm, kw in V:
        kw = dict(dict(stop=1.5, mult=1.0, check_every=1), **kw)
        cfg.append((nm, [L2Gated(0.30, **kw)], 1e8, False))
    rows = []
    for nm, (E, T, a) in rt.replay_many(cfg).items():
        r = dict(name=nm, **week_pf(T, info))
        rows.append(r)
        print(r, flush=True)
    pd.DataFrame(rows).to_csv("data/reports/realtrade_s7_l2.csv", index=False)


def stage_port(l2_kw):
    import research_realtrade_s5_portfolio as s5p
    rt.MARGIN_PCT = DHAN_MARGIN
    rt.RUNNING = R.MONTHLY_RUNNING_COSTS
    rows = []
    for cap in (1_500_000, 2_500_000):
        cfg = [
            ("S5 as bot ran it", [rt.StaggeredStrangle(0.30, stop=1.5, check_every=5, min_total=3),
                                  rt.GapBuy(0.03, vix_max=15, target="close", vix_src="0920", check_every=5),
                                  s5p.Monthly("real")]),
            ("S6", s6()),
            ("S6 + MON 0.2", s6() + [MonthlyReal(0.20)]),
            ("S6 + L2g 0.3", s6() + [L2Gated(0.30, **l2_kw)]),
            ("S6 + L2g 0.3 + MON 0.2", s6() + [L2Gated(0.30, **l2_kw), MonthlyReal(0.20)]),
            ("S6 0.3/0.3 + L2g 0.3 + MON 0.2", s6(0.3, 0.3) + [L2Gated(0.30, **l2_kw), MonthlyReal(0.20)]),
            ("S6 + MON 0.3", s6() + [MonthlyReal(0.30)]),
            ("S6 gap 8% + MON 0.2", s6(fg=0.08) + [MonthlyReal(0.20)]),
        ]
        for nm, (E, T, a) in rt.replay_many([(f"{n} @{cap // 100000}L", s, cap, True) for n, s in cfg]).items():
            r = rt.report(nm, E, T, a, cap)
            rt.show(r)
            rows.append(r)
    pd.DataFrame(rows).to_csv("data/reports/realtrade_s7_port.csv", index=False)


if __name__ == "__main__":
    if sys.argv[1] == "l2":
        stage_l2()
    else:
        import json
        stage_port(json.loads(sys.argv[2]) if len(sys.argv) > 2 else dict(vix_min=15, stop=1.5, mult=1.0))
