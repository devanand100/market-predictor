"""
Option BUYING research, part 2: positional long premium on the nearest weekly expiry.

Structures (all bought at 09:30, contract-exact via research_positional.run_cycle):
  straddle    long ATM CE + PE
  strangle    long CE ATM+100, PE ATM-100
  dir_*       long ATM CE or PE in a signal's direction:
                conf   = Strategy 2's confirmed signal (gap and FII option change agree)
                gap    = opening gap sign
                mom5   = 5-day momentum sign
                bull / bear / random baselines
Entry DTE 4 (first session of a full week), 1 or 0 (expiry day). Exit at the first of stop
(MTM <= -stop x debit), target (MTM >= tp x debit) or 15:15 on expiry day.

Return per trade = net Rs at 1 lot of rp.QTY / debit Rs (costs included; conservative at larger
size because brokerage is per order). Sized equity: spend `f` of equity on the debit per trade.

Writes data/reports/option_buying_positional.csv (per trade) and _grid.csv.
"""
import itertools
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily, atm_strike

IS_END = "2023-12-31"
DTES = [4, 1, 0]
STOPS = [None, 0.4]
TPS = [None, 0.5, 1.0]


def legs(kind, direction):
    def f(spot):
        K = atm_strike(spot)
        if kind == "straddle":
            return [("CE", K, 1), ("PE", K, 1)]
        if kind == "strangle":
            return [("CE", K + 100, 1), ("PE", K - 100, 1)]
        return [("CE", K, 1)] if direction > 0 else [("PE", K, 1)]
    return f


def directions(sig, rng):
    g, fo = np.sign(sig["pa_gap"]), np.sign(sig["fii_opt_chg"])
    return {
        "conf": g.where((g == fo) & (fo != 0), 0),
        "gap": g,
        "mom5": np.sign(sig["pa_mom5"]),
        "bull": pd.Series(1, index=sig.index),
        "bear": pd.Series(-1, index=sig.index),
        "random": pd.Series(rng.choice([-1, 1], len(sig)), index=sig.index),
    }


def main():
    days, daily = load_days(), load_daily()
    cyc = rp.cycles(daily, days)
    sig = pd.read_csv("data/reports/weekly_signals.csv", index_col="entry")
    dirs = directions(sig, np.random.default_rng(1))
    rows = []
    for window, exp in cyc:
        for dte in DTES:
            if len(window) < dte + 1:
                continue
            w = window[-(dte + 1):]
            # Special sessions (e.g. Muhurat 2025-10-21) have no 09:30 prices; a long leg would
            # be "bought" at the 0.05 intrinsic fallback. run_cycle only guards short legs.
            if np.isnan(days[w[0]].ce[rp.ENTRY_MIN]).all():
                continue
            e = pd.Timestamp(w[0])
            f = daily.loc[e]
            prev = daily.iloc[daily.index.get_loc(e) - 1]
            feats = dict(vix=f["vix_0920"], vix_chg=f["vix_0920"] / prev["vix_close"] - 1,
                         iv_rv=f["vix_0920"] / max(prev["rv5"], 1e-6), full_week=len(window) == 5)
            plans = [("straddle", 0, "-"), ("strangle", 0, "-")]
            for name, s in dirs.items():
                d = s.get(window[0], 0)   # signals are defined at the cycle's first session
                if d != 0:
                    plans.append(("dir", int(d), name))
            for (kind, d, sname), stop, tp in itertools.product(plans, STOPS, TPS):
                r = rp.run_cycle(days, w, legs(kind, d), stop, tp)
                if not r:
                    continue
                debit = -r["credit"] * rp.QTY
                rows.append(dict(entry=w[0], expiry=exp, dte=dte, kind=kind, sig=sname, d=d,
                                 stop=str(stop), tp=str(tp), reason=r["reason"],
                                 ret=r["rs"] / debit, **feats))
    T = pd.DataFrame(rows)
    T.to_csv("data/reports/option_buying_positional.csv", index=False)
    return T


FILTERS = {
    "all": lambda t: t,
    "vix>15": lambda t: t[t.vix > 15],
    "vix<=15": lambda t: t[t.vix <= 15],
    "iv<rv": lambda t: t[t.iv_rv < 1.0],
    "vix_up5%": lambda t: t[t.vix_chg > 0.05],
}


def sized(r, f=0.05):
    e = np.cumprod(1 + f * r.to_numpy())
    dd = (e / np.maximum.accumulate(e) - 1).min()
    return e[-1], dd


def grid(T):
    out = []
    for k, g in T.groupby(["dte", "kind", "sig", "stop", "tp"]):
        for fn, fl in FILTERS.items():
            s = fl(g).sort_values("entry")
            row = dict(zip(["dte", "kind", "sig", "stop", "tp"], k), filt=fn, n=len(s))
            for per, p in [("IS", s[s.entry <= IS_END]), ("OOS", s[s.entry > IS_END])]:
                yrs = 3.0 if per == "IS" else 2.75
                if len(p) < 10:
                    row.update({f"{per}_n": len(p)})
                    continue
                fin, dd = sized(p.ret)
                row.update({f"{per}_n": len(p), f"{per}_avg": round(100 * p.ret.mean(), 1),
                            f"{per}_win": round(100 * (p.ret > 0).mean()),
                            f"{per}_cagr5": round(100 * (fin ** (1 / yrs) - 1), 1),
                            f"{per}_dd5": round(100 * dd, 1)})
            out.append(row)
    return pd.DataFrame(out)


if __name__ == "__main__":
    T = main()
    G = grid(T)
    G.to_csv("data/reports/option_buying_positional_grid.csv", index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    print("Median avg net return per trade (% of debit) by structure x filter:")
    print(G.groupby(["kind", "filt"])[["IS_avg", "OOS_avg"]].median().round(1).unstack("filt"))
    print("\nBest straddle/strangle cells by IS avg, with OOS:")
    v = G[G.kind != "dir"].sort_values("IS_avg", ascending=False).head(12)
    print(v.to_string(index=False))
    print("\nDirectional buys, no stop/tp, by signal (avg % of debit):")
    d = G[(G.kind == "dir") & (G.stop == "None") & (G.tp == "None") & (G.filt.isin(["all", "vix>15"]))]
    print(d[["dte", "sig", "filt", "IS_n", "IS_avg", "IS_win", "OOS_n", "OOS_avg", "OOS_win"]].to_string(index=False))
    print("\nBest directional cells by IS avg (n>=30), with OOS:")
    v = G[(G.kind == "dir") & (G.IS_n >= 30)].sort_values("IS_avg", ascending=False).head(12)
    print(v.to_string(index=False))
