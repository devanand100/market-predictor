"""
Regime switch: sell premium when options are expensive, buy when they are cheap.

Every full week (entry 09:30, 4 sessions before expiry) look at VIX at 09:20:
  VIX >  T  -> Strategy 1 strangle (sell CE/PE at ATM +/- straddle, 1.5x credit stop)
  VIX <= T  -> Strategy 3 gap buy (ATM CE/PE in the gap direction, +100% target, no stop)
Also tested: each sleeve alone, both every week ("stack"), and a VIX/RV20 ratio split.

One Rs 3L account, lot-exact (65), compounding. Strangle lots = floor(margin_use x equity /
(9% x spot x 65)); gap buy spends `frac` of equity on premium (1 lot allowed up to 2 x frac).
T, margin_use and frac are judged on 2021-23; 2024-26 is out-of-sample.

Writes data/reports/regime_switch_grid.csv, regime_switch_trades.csv (chosen config)
"""
import itertools
import numpy as np
import pandas as pd

import final_strategy as fs
import final_gap_buy as gb
from cost_model import option_fill_cost, MONTHLY_RUNNING_COSTS

START = 300_000
LOT = 65
RUNNING = sum(MONTHLY_RUNNING_COSTS.values())
IS_END = "2023-12-31"


def week_table():
    s1, daily = fs.trades()
    s1 = s1.set_index("entry")
    s3 = gb.trades(1.0).set_index("entry", drop=False)
    wk = pd.DataFrame(index=sorted(set(s1.index) | set(s3.index)))
    wk["vix"] = [daily.at[pd.Timestamp(e), "vix_0920"] for e in wk.index]
    wk["rv20"] = [daily["rv20"].shift(1).get(pd.Timestamp(e), np.nan) for e in wk.index]
    return wk, s1, s3


def gap_pnl(r, eq, frac):
    per_lot = r.buy * LOT
    lots = int(frac * eq // per_lot) or (1 if per_lot <= 2 * frac * eq else 0)
    if not lots:
        return 0.0, 0
    q = lots * LOT
    return (r.sell - r.buy) * q - option_fill_cost(r.buy, q, True, r.entry) \
        - option_fill_cost(r.sell, q, False, r.exit), lots


def strangle_pnl(r, eq, use):
    lots = int(use * eq // (fs.MARGIN_PCT * r.spot * LOT))
    return r.rs * lots, lots


def simulate(wk, s1, s3, rule, T=15.0, use=0.6, frac=0.05, running=False, start=START, ratio=1.1):
    eq, last_m, rows = float(start), None, []
    for e, w in wk.iterrows():
        sell = buy = False
        if rule == "strangle":
            sell = True
        elif rule == "gap":
            buy = True
        elif rule == "gap_lowvix":
            buy = w.vix <= T
        elif rule == "switch":
            sell, buy = w.vix > T, w.vix <= T
        elif rule == "stack":
            sell = buy = True
        elif rule == "stack_lowvix":           # strangle every week, gap buy only when VIX <= T
            sell, buy = True, w.vix <= T
        elif rule == "ratio_switch":           # rich options (VIX/RV20 >= ratio) -> sell
            rich = w.vix / w.rv20 >= ratio if w.rv20 == w.rv20 else False
            sell, buy = rich, not rich
        pnl, what = 0.0, []
        if sell and e in s1.index:
            p, n = strangle_pnl(s1.loc[e], eq, use)
            pnl += p
            what.append(f"strangle x{n}")
        if buy and e in s3.index:
            p, n = gap_pnl(s3.loc[e], eq, frac)
            pnl += p
            what.append(f"gap {s3.at[e, 'typ']} x{n}")
        fixed = 0.0
        if running and e[:7] != last_m:
            fixed, last_m = RUNNING, e[:7]
        before = eq
        eq += pnl - fixed
        rows.append(dict(entry=e, vix=w.vix, trade=" + ".join(what), pnl=pnl - fixed,
                         ret=(pnl - fixed) / before, equity=eq))
    return pd.DataFrame(rows)


def metrics(led, start, d0, d1):
    e = np.concatenate([[start], led["equity"].to_numpy()])
    yrs = (pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25
    r = led["ret"]
    return dict(cagr=round(100 * ((e[-1] / start) ** (1 / yrs) - 1), 1),
                dd=round(100 * (e / np.maximum.accumulate(e) - 1).min(), 1),
                sharpe=round(r.mean() / r.std() * np.sqrt(52), 2) if r.std() > 0 else np.nan,
                worst=round(100 * r.min(), 1), active=int((led.trade != "").sum()))


def evaluate(led):
    first, last = led.entry.iloc[0], led.entry.iloc[-1]
    is_ = led[led.entry <= IS_END]
    oos = led[led.entry > IS_END]
    out = {f"all_{k}": v for k, v in metrics(led, START, first, last).items()}
    out.update({f"is_{k}": v for k, v in metrics(is_, START, first, IS_END).items()})
    out.update({f"oos_{k}": v for k, v in metrics(oos, is_.equity.iloc[-1], "2024-01-01", last).items()})
    return out


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    wk, s1, s3 = week_table()
    rows = []
    cfgs = [("strangle", None, u, None, None) for u in [0.6, 0.8]]
    cfgs += [("gap", None, None, f, None) for f in [0.05, 0.075, 0.10]]
    cfgs += [("gap_lowvix", 15, None, f, None) for f in [0.05, 0.075, 0.10]]
    cfgs += [("switch", T, u, f, None) for T, u, f in itertools.product([13, 14, 15, 16, 17], [0.6, 0.8], [0.05, 0.075, 0.10])]
    cfgs += [("stack", None, u, f, None) for u, f in itertools.product([0.6], [0.05, 0.075])]
    cfgs += [("ratio_switch", None, u, f, q) for u, f, q in itertools.product([0.6, 0.8], [0.05, 0.075], [1.0, 1.1, 1.2])]
    for rule, T, u, f, q in cfgs:
        led = simulate(wk, s1, s3, rule, T=T or 15, use=u or 0.6, frac=f or 0.05, ratio=q or 1.1)
        rows.append(dict(rule=rule, T=T, use=u, frac=f, ratio=q, **evaluate(led)))
    G = pd.DataFrame(rows)
    G.to_csv("data/reports/regime_switch_grid.csv", index=False)
    print(G.to_string(index=False))
