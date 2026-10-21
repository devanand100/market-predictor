"""
Structure search on the real-price replay (research_realtrade.py): every candidate trades 1 lot
(lot size of the date) in its own account, no running costs, so the per-trade profit factor and
the Rs per lot are directly comparable. Selection is on 2021-2023 (IS); 2024+ is reported as OOS.

Writes data/reports/realtrade_grid.csv
"""
import sys
import numpy as np
import pandas as pd

import research_realtrade as rt


def rnd(x):
    return max(50, int(round(x / 50)) * 50)


def strangle(mult):
    return lambda S, K, st, d: [("CE", K + rnd(mult * st), -1, 1), ("PE", K - rnd(mult * st), -1, 1)]


def straddle():
    return lambda S, K, st, d: [("CE", K, -1, 1), ("PE", K, -1, 1)]


def condor(mult, wing):
    def f(S, K, st, d):
        w = rnd(mult * st) if mult else 0
        return [("CE", K + w, -1, 1), ("PE", K - w, -1, 1), ("CE", K + w + wing, 1, 1), ("PE", K - w - wing, 1, 1)]
    return f


def long_dir(off=0):
    return lambda S, K, st, d: [("CE" if d > 0 else "PE", K + d * off, 1, 1)]


def debit_dir(width, off=0):
    def f(S, K, st, d):
        t = "CE" if d > 0 else "PE"
        return [(t, K + d * off, 1, 1), (t, K + d * (off + width), -1, 1)]
    return f


def credit_dir(off, width):
    """Sell the OTM option on the side AGAINST the direction (gap up -> bull put spread)."""
    def f(S, K, st, d):
        t = "PE" if d > 0 else "CE"
        return [(t, K - d * off, -1, 1), (t, K - d * (off + width), 1, 1)]
    return f


def candidates():
    W = rt.Weekly
    c = []
    for dte in (4, 3, 2, 1, 0):
        for stop in (None, 1.0, 2.0):
            c.append(W(f"strangle1.0 dte{dte} sl{stop}", strangle(1.0), dte=dte, stop=stop))
            c.append(W(f"condor1.0w200 dte{dte} sl{stop}", condor(1.0, 200), dte=dte, stop=stop))
            c.append(W(f"ironfly w300 dte{dte} sl{stop}", condor(0, 300), dte=dte, stop=stop))
        c.append(W(f"strangle0.5 dte{dte} sl1.0", strangle(0.5), dte=dte, stop=1.0))
        c.append(W(f"straddle dte{dte} sl0.5", straddle(), dte=dte, stop=0.5))
        for tp in (0.5, 1.0, 2.0):
            c.append(W(f"gaplong atm dte{dte} tp{tp}", long_dir(0), dte=dte, direction="gap", tp=tp, tp_limit=True))
        c.append(W(f"gaplong atm dte{dte} hold", long_dir(0), dte=dte, direction="gap"))
        c.append(W(f"gaplong otm100 dte{dte} tp1.0", long_dir(100), dte=dte, direction="gap", tp=1.0, tp_limit=True))
        c.append(W(f"gapdebit 200 dte{dte} tp0.8", debit_dir(200), dte=dte, direction="gap", tp=0.8))
        c.append(W(f"gapdebit 200 dte{dte} hold", debit_dir(200), dte=dte, direction="gap"))
        c.append(W(f"gapcredit off100 w200 dte{dte}", credit_dir(100, 200), dte=dte, direction="gap"))
        c.append(W(f"gapcredit off0 w200 dte{dte}", credit_dir(0, 200), dte=dte, direction="gap"))
        c.append(W(f"bullcredit off100 w200 dte{dte}", credit_dir(100, 200), dte=dte, direction="bull"))
        c.append(W(f"bearcredit off100 w200 dte{dte}", credit_dir(100, 200), dte=dte, direction="bear"))
    return c


def summarize(name, T):
    T = T[T.sleeve != "running"].copy()
    if T.empty:
        return dict(name=name, n=0)
    T["lotsz"] = T.entry.map(rt.get_lot_size)
    T["per_lot"] = T.pnl / T.lots
    T["per_unit"] = T.per_lot / T.lotsz
    I, O = T[T.entry <= rt.IS_END], T[T.entry > rt.IS_END]
    r = dict(name=name, n=len(T), pf=rt.pf(T.pnl), is_n=len(I), is_pf=rt.pf(I.pnl), oos_pf=rt.pf(O.pnl),
             is_pts=round(I.per_unit.mean(), 2), oos_pts=round(O.per_unit.mean(), 2),
             win=round(100 * (T.pnl > 0).mean(), 1), worst_pts=round(T.per_unit.min(), 1))
    for y, g in T.groupby(T.entry.str[:4]):
        r[y] = round(g.per_unit.sum(), 0)
    return r


if __name__ == "__main__":
    C = candidates()
    print(len(C), "candidates", flush=True)
    rows = []
    B = 25
    for k in range(0, len(C), B):
        res = rt.replay_many([(c.sleeve, [c], 1e8, False) for c in C[k:k + B]])
        for nm, (E, T, a) in res.items():
            r = summarize(nm, T)
            r["unobs"] = a.unobserved
            rows.append(r)
        print(k + B, "done", flush=True)
        pd.DataFrame(rows).to_csv("data/reports/realtrade_grid.csv", index=False)
    R = pd.DataFrame(rows)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)
    print(R.sort_values("is_pf", ascending=False).to_string())
