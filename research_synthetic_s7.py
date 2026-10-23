"""
Strategy 7 (final_realtrade_s7.py: Strategy 6 + monthly 45->21 DTE short straddle) on synthetic
2019-11 .. 2020-12 option prices, the COVID stress test of research_synthetic_options.py extended to MON.

Weekly sleeves use research_synthetic_options (BS + weekly IV/VIX surface). MON needs its own surface:
monthly options at 21-49 calendar DTE, fitted the same way on the real 2021-2023 hourly monthly chains
(research_monthly_straddle.load_contracts): IV / v by calendar DTE (5 buckets) x VIX (5 buckets) x
z = ln(K/F)/(v sqrt T), T = calendar time to 15:30 of expiry / 365 (the monthly research convention).

Synthetic MON trades follow research_monthly_straddle.run_trade: enter at ~10:15 (minute 60) on the first
session at <= 45 calendar DTE (skip if that is < 41), strike nearest the forward, short CE+PE with slippage
and statutory costs per lot of 65; marked at each session close; bought back at ~10:15 on the first session
at <= 21 DTE. Monthly expiry before 2021 = the last weekly (rule) expiry of the month.

Usage:
  python research_synthetic_s7.py calibrate   # -> data/reports/synthetic_monthly_iv_surface.json
  python research_synthetic_s7.py check       # synthetic vs real MON trades 2021-26; S7 replay synthetic vs real 2021-26
  python research_synthetic_s7.py stress      # S7 on 2019-11..2020-12 (+ stress cases) and chained with real 2021+
"""
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

import cost_model
import engine
import research_monthly_straddle as ms
import research_monthly_straddle_vix as mv
import research_realtrade as rt
import research_synthetic_options as so
from cost_model import option_fill_cost
from research_realtrade_s7 import MonthlyReal, s6

sys.path.insert(0, "strategy6_bot")
import rules as R  # noqa: E402

REP = so.REP
MSURF = REP + "synthetic_monthly_iv_surface.json"
DTE_EDGES = [25, 32, 39, 46]
# Strategy 7's MON uses real-only prices: an exit leg with no traded quote is priced at its last real price
# moved by intrinsic, plus urgent slippage. On 2021-26 that happened when the strike ended > ~450 pts from spot
# (22 of 25 such exits), and cost a median 1.234 x the fair (synthetic) exit value. Same haircut here.
STALE_PTS, STALE_MULT = 450, 1.234
START, W_MON, MARGIN = 1_500_000, 0.20, 0.116


# ------------------------------------------------------------------ monthly model
class MonthlyModel:
    def __init__(self, path=MSURF):
        with open(path) as f:
            js = json.load(f)
        self.R = np.array(js["ratio"])         # [dte bucket, vix bucket, z]
        self.r = js["r"]
        self.hi_mult, self.trend = 1.0, False

    def price(self, ce, K, S, T, v):
        F = S * np.exp(self.r * T)
        vv = v / 100.0
        z = np.log(K / F) / (vv * np.sqrt(T))
        d = int(np.searchsorted(DTE_EDGES, T * 365, side="right"))
        b = int(np.searchsorted(so.VIX_EDGES, v, side="right"))
        sig = vv * np.interp(z, so.ZC, self.R[d, b])
        if v > so.VIX_EDGES[-1]:
            sig *= self.hi_mult
            if self.trend:
                sig *= (v / 22.5) ** 0.15
        return float(max(0.05, round(so.black(ce, K, F, T, sig, self.r) / 0.05) * 0.05))


def t_cal(d, m, expiry):
    """Calendar years from minute m (0 = 09:15) of session d to 15:30 of expiry."""
    end = pd.Timestamp(expiry) + pd.Timedelta(hours=15, minutes=30)
    now = pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15 + int(m))
    return max((end - now).total_seconds() / (365 * 86400), 1e-6)


_V = {}


def vix_at(d, m, prev, beta):
    if d not in _V:
        S = pd.DataFrame(so.spot_minutes()[d]).bfill().to_numpy()[:, 3]
        _V[d] = (S, so.vix_path(d, prev[d], S, beta))
    S, v = _V[d]
    return S[m], v[m]


def calibrate():
    beta = so.Model().beta
    r = so.Model().r
    daily = so.daily_frame()
    idx = list(daily.index.strftime("%Y-%m-%d"))
    prev = {d: idx[i - 1] for i, d in enumerate(idx) if i}
    chains, exps = ms.load_contracts()
    rows = []
    for e, ch in chains.items():
        if e > so.CAL_END:
            continue
        for ts, c in ch.items():
            d = str(ts.date())
            if d not in prev or d not in so.spot_minutes():
                continue
            m = min(ts.hour * 60 + ts.minute + 60 - 555, 375)
            T = t_cal(d, m, e)
            if not (18 / 365 <= T <= 50 / 365):
                continue
            F = c.forward()
            if F is None:
                continue
            _, v = vix_at(d, m, prev, beta)
            for (K, typ), p in c.px.items():
                if (typ == "CE") != (K >= F) or abs(K - F) > 1500 or p < 1:
                    continue
                rows.append((T, v, K, F, typ == "CE", p))
    a = np.array(rows, dtype=float)
    T, v, K, F, ce, p = a.T
    ce = ce.astype(bool)
    iv = so.implied_vol(ce, p, K, F, T, r)
    good = (iv > 0.011) & (iv < 3.9)
    vv = v / 100
    z = np.log(K / F) / (vv * np.sqrt(T))
    df = pd.DataFrame(dict(d=np.searchsorted(DTE_EDGES, T * 365, side="right"),
                           b=np.searchsorted(so.VIX_EDGES, v, side="right"),
                           z=np.clip(np.round((z + 4) / 0.25).astype(int), 0, len(so.ZC) - 1), ratio=iv / vv))[good]
    g = df.groupby(["d", "b", "z"]).ratio.agg(["median", "size"]).reset_index()
    Rr = np.full((5, 5, len(so.ZC)), np.nan)
    for row in g.itertuples():
        if row[5] >= 10:
            Rr[row.d, row.b, row.z] = row[4]
    for d in range(5):
        for b in range(5):
            f = np.isfinite(Rr[d, b])
            if f.any():
                Rr[d, b] = np.interp(so.ZC, so.ZC[f], Rr[d, b][f])
    for d in range(5):
        for b in range(5):
            if np.isnan(Rr[d, b]).all():
                bb = min((x for x in range(5) if np.isfinite(Rr[d, x]).all()), key=lambda x: abs(x - b))
                Rr[d, b] = Rr[d, bb]
    assert np.isfinite(Rr).all()
    with open(MSURF, "w") as f:
        json.dump(dict(r=r, fit=f"2021-01-01..{so.CAL_END}", dte_edges=DTE_EDGES, ratio=Rr.round(4).tolist()), f)
    print(f"{good.sum()} monthly quotes; ATM (z=0) IV/VIX by calendar DTE x VIX:")
    print(pd.DataFrame(Rr[:, :, 16].round(2), index=["<=25", "26-32", "33-39", "40-46", "47+"],
                       columns=["<12", "12-14", "14-17", "17-21", "21+"]).to_string())
    print("skew 40-46 DTE VIX 14-17, z=-3..3:", Rr[3, 2, 4:29:4].round(2).tolist())


# ------------------------------------------------------------------ synthetic MON trades
def monthly_expiries_pre2021(daily):
    idx = daily.index.strftime("%Y-%m-%d")
    e = pd.Series(idx[(daily.is_expiry == True).to_numpy() & (idx < "2021-01-01")])
    return list(e.groupby(e.str[:7]).max())


def synth_monthly(expiries, sessions, daily, mm, beta, stale_mult=STALE_MULT):
    idx = list(daily.index.strftime("%Y-%m-%d"))
    prev = {d: idx[i - 1] for i, d in enumerate(idx) if i}
    slip = cost_model.slip
    out = []
    for e in expiries:
        ss = [d for d in sessions if d <= e and d in prev]
        dte = lambda d: (date.fromisoformat(e) - date.fromisoformat(d)).days
        exit_d = next((d for d in ss if dte(d) <= ms.EXIT_DTE), None)
        entry_d = next((d for d in ss if dte(d) <= ms.ENTRY_DTE), None)
        if not (entry_d and exit_d) or dte(entry_d) < ms.ENTRY_DTE - 4:
            continue
        S0, v0 = vix_at(entry_d, 60, prev, beta)
        T0 = t_cal(entry_d, 60, e)
        K = engine.atm_strike(S0 * np.exp(mm.r * T0))
        ce, pe = mm.price(True, K, S0, T0, v0), mm.price(False, K, S0, T0, v0)
        credit = (ce - slip(ce)) + (pe - slip(pe))
        cost = option_fill_cost(ce, ms.LOT, False, entry_d) + option_fill_cost(pe, ms.LOT, False, entry_d)
        mtm, worst = {}, 0.0
        for d in [d for d in ss if entry_d <= d <= exit_d]:
            m = 60 if d == exit_d else 375
            S, v = vix_at(d, m, prev, beta)
            T = t_cal(d, m, e)
            pc, pp = mm.price(True, K, S, T, v), mm.price(False, K, S, T, v)
            if d == exit_d:
                stale = abs(S - K) > STALE_PTS
                k = stale_mult if stale else 1.0
                pts = credit - k * (pc + pp) - slip(pc, stale) - slip(pp, stale)
                cost += option_fill_cost(pc, ms.LOT, True, d) + option_fill_cost(pp, ms.LOT, True, d)
                mtm[d] = pts
            else:
                mtm[d] = credit - pc - pp
                worst = min(worst, mtm[d])
        out.append(dict(expiry=e, entry=entry_d, exit=exit_d, strike=K, spot=S0, credit=credit, pts_real=pts,
                        rs_real=pts * ms.LOT - cost, worst_pts=worst, mtm=mtm))
    t = pd.DataFrame(out)
    f = mv.features(engine.load_daily(), mv.load_vix())
    t["vrp"] = f.reindex(pd.to_datetime(t.entry), method="ffill")["vrp"].to_numpy()
    return t


def mon_sleeve(trades, w=W_MON):
    s = MonthlyReal(w)
    s.by_entry = {r.entry: r for r in trades[trades.vrp >= 2].itertuples()}
    return s


# ------------------------------------------------------------------ replay
def run(days, daily, mon, d0, d1, start=START, slip_mult=1.0, margin=MARGIN, w_mon=W_MON):
    base, saved, run0 = cost_model.slip, dict(rt._DATA), rt.RUNNING
    rt.MARGIN_PCT, rt.RUNNING = margin, R.MONTHLY_RUNNING_COSTS
    rt.slip = lambda p, urgent=False: slip_mult * base(p, urgent)
    rt._DATA.clear()
    rt._DATA.update(days=days, daily=daily)
    try:
        sl = s6() + ([mon_sleeve(mon, w_mon)] if w_mon else [])
        E, T, acct = rt.replay(sl, start, True, d0, d1)
    finally:
        rt.slip, rt.MARGIN_PCT, rt.RUNNING = base, 0.09, run0
        rt._DATA.clear()
        rt._DATA.update(saved)
    T = T.drop(columns=[c for c in ("row", "mtm") if c in T])
    return E, T, acct


def real_mon():
    return pd.read_pickle("data/reports/monthly_realonly_trades.pkl")


def check():
    mm, beta = MonthlyModel(), so.Model().beta
    daily = so.daily_frame()
    real = real_mon()
    sessions = [d for d in daily.index.strftime("%Y-%m-%d") if d >= "2021-01-01" and d in so.spot_minutes()]
    pf = lambda x: round(x[x > 0].sum() / -x[x < 0].sum(), 2)
    fair = synth_monthly(list(real.expiry), sessions, daily, mm, beta, 1.0)
    jf = real.merge(fair, on="expiry", suffixes=("_real", "_syn"))
    jf = jf[jf.vrp_real >= 2]
    print(f"MON VRP>=2 fair synthetic exits: Rs/lot syn {jf.rs_real_syn.sum():,.0f} vs research (parity) "
          f"{jf.rs_research.sum():,.0f} vs real-only {jf.rs_real_real.sum():,.0f}")
    syn = synth_monthly(list(real.expiry), sessions, daily, mm, beta)
    j = real.merge(syn, on="expiry", suffixes=("_real", "_syn"))
    for nm, g in [("all", j), ("VRP>=2 (real filter)", j[j.vrp_real >= 2])]:
        print(f"MON {nm}: n {len(g)}; credit syn/real {(g.credit_syn / g.credit_real).median():.2f}; "
              f"pts corr {np.corrcoef(g.pts_real_real, g.pts_real_syn)[0, 1]:.2f}; Rs/lot real {g.rs_real_real.sum():,.0f} "
              f"syn {g.rs_real_syn.sum():,.0f}; PF real {pf(g.rs_real_real)} syn {pf(g.rs_real_syn)}; "
              f"same entry {(g.entry_real == g.entry_syn).mean():.0%}")
    for p, g in j.groupby(j.expiry <= so.CAL_END):
        print(f"  {'2021-23' if p else '2024+'}: credit syn/real {(g.credit_syn / g.credit_real).median():.2f}, "
              f"Rs/lot real {g.rs_real_real.sum():,.0f} syn {g.rs_real_syn.sum():,.0f}")
    j.drop(columns=["mtm_real", "mtm_syn"]).to_csv(REP + "synthetic_monthly_check.csv", index=False)
    wk = so.synth_days(sorted(engine.load_days()), daily, so.Model())
    out = {}
    for name, days, mon in [("S7 real 2021-26", engine.load_days(), real), ("S7 synthetic 2021-26", wk, syn)]:
        E, T, acct = run(days, daily, mon, "2021-01-01", "2099")
        s = so.summary(name, E, T, acct, START)
        so.show(s)
        out[name] = s
    with open(REP + "synthetic_s7_check.json", "w") as f:
        json.dump(out, f, indent=1, default=str)


def stress():
    beta = so.Model().beta
    daily = so.daily_frame()
    pre = so.trading_dates_pre2021()
    exps = monthly_expiries_pre2021(daily)
    out = {}

    def models(mult=1.0, trend=False, stale=STALE_MULT):
        w, m = so.Model(), MonthlyModel()
        w.hi_mult, w.trend, m.hi_mult, m.trend = mult, trend, mult, trend
        return so.synth_days(pre, daily, w), synth_monthly(exps, pre, daily, m, beta, stale)

    wk, mon = models()
    print("synthetic MON trades 2019-20:")
    print(mon.drop(columns="mtm").round(1).to_string(index=False))
    cases = [("base 15L", {}), ("without MON (Strategy 6 @15L, 11.6%)", dict(w_mon=0)), ("slippage x2", dict(slip_mult=2.0)),
             ("slippage x3", dict(slip_mult=3.0)), ("margin 15%", dict(margin=0.15)), ("start 10L", dict(start=1_000_000))]
    for name, kw in cases:
        E, T, acct = run(wk, daily, mon, "2019-11-18", "2020-12-31", **kw)
        s = so.summary("S7 2019-20 syn " + name, E, T, acct, kw.get("start", START))
        so.show(s)
        out[s["name"]] = s
        if name == "base 15L":
            T.to_csv(REP + "synthetic_s7_2019_20_trades.csv", index=False)
            E.to_csv(REP + "synthetic_s7_2019_20_equity.csv", index=False)
            Em = E.assign(month=E.date.str[:7]).groupby("month").equity.last()
            print("monthly returns %:", ((Em / Em.shift(1).fillna(START) - 1) * 100).round(1).to_dict())
            tr = T[T.sleeve != "running"]
            print(tr.nsmallest(8, "pnl")[["sleeve", "entry", "exit", "reason", "pnl", "credit", "lots"]].to_string(index=False))
    E, T, acct = run(wk, daily, models(stale=1.0)[1], "2019-11-18", "2020-12-31")
    s = so.summary("S7 2019-20 syn MON fair exits", E, T, acct, START)
    so.show(s)
    out[s["name"]] = s
    for name, mult, trend, kw in [("IV x0.85 when VIX>21", 0.85, False, {}), ("IV trend above VIX 21", 1.0, True, {}),
                                  ("slippage x3 + margin 15%", 1.0, False, dict(slip_mult=3.0, margin=0.15)),
                                  ("IV x0.85 + slippage x3 + margin 15%", 0.85, False, dict(slip_mult=3.0, margin=0.15))]:
        w2, m2 = (wk, mon) if (mult, trend) == (1.0, False) else models(mult, trend)
        E, T, acct = run(w2, daily, m2, "2019-11-18", "2020-12-31", **kw)
        s = so.summary("S7 2019-20 syn " + name, E, T, acct, START)
        so.show(s)
        out[s["name"]] = s
    days = dict(wk)
    days.update(engine.load_days())
    mon_all = pd.concat([mon, real_mon()], ignore_index=True)
    E, T, acct = run(days, daily, mon_all, "2019-11-18", "2099")
    s = so.summary("S7 chained syn 2019-20 + real 2021+", E, T, acct, START)
    so.show(s)
    out[s["name"]] = s
    with open(REP + "synthetic_s7_stress_summary.json", "w") as f:
        json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("calibrate", "all"):
        calibrate()
    if what in ("check", "all"):
        check()
    if what in ("stress", "all"):
        stress()
