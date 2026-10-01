"""
Refinement of the surviving edge: positional weekly short strangle.

Strikes are placed at +/- m x (ATM straddle premium) from spot (expected-move based, so the
structure is comparable at Nifty 14k and 26k, VIX 10 and 25). Optional long wings at the
edge of the data window (+/-500 from ATM, the farthest strikes with real prices).
Filters (pre-registered, economically motivated only):
  vrp  : trade only when VIX(09:20) / RV20 >= threshold (options are rich vs realised)
  vixmax: skip entries when VIX is above a panic level
Parameters are selected on 2021-2023 ONLY.
"""
import itertools
import numpy as np
import pandas as pd

from engine import load_days, load_daily, atm_strike
from research_positional import cycles, run_cycle, stats, ENTRY_MIN

WINDOW = 500


def make_legs(day, m, width_mult, hedge):
    def f(spot):
        K = atm_strike(spot)
        straddle = day.px(K, "CE", m) + day.px(K, "PE", m)
        w = max(50, int(round(width_mult * straddle / 50.0)) * 50)
        w = min(w, WINDOW - 100)
        legs = [("CE", K + w, -1), ("PE", K - w, -1)]
        if hedge:
            legs += [("CE", K + WINDOW, +1), ("PE", K - WINDOW, +1)]
        return legs
    return f


def backtest(days, daily, cyc, dte_entry, width_mult, stop_mult, tp, hedge, vrp_min=None, vix_max=None):
    rows = []
    for window, exp in cyc:
        if len(window) < dte_entry + 1:
            continue
        w = window[-(dte_entry + 1):]
        ts = pd.Timestamp(w[0])
        vix = daily.at[ts, "vix_0920"] if ts in daily.index else np.nan
        rv = daily["rv20"].shift(1).get(ts, np.nan)
        if vix_max and vix > vix_max:
            continue
        if vrp_min and (np.isnan(vix) or np.isnan(rv) or vix / rv < vrp_min):
            continue
        r = run_cycle(days, w, make_legs(days[w[0]], ENTRY_MIN, width_mult, hedge), stop_mult, tp)
        if r:
            r["vix"] = vix
            r["rv20"] = rv
            rows.append(r)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    days = load_days()
    daily = load_daily()
    daily["rv20_prev"] = daily["rv20"].shift(1)
    cyc = cycles(daily, days)
    rows = []
    grid = itertools.product([2, 3, 4], [0.5, 0.75, 1.0], [1.5, 2.5, None], [None, 0.6], [False, True],
                             [None, 1.1])
    for dte_e, wm, sm, tp, hedge, vrp in grid:
        df = backtest(days, daily, cyc, dte_e, wm, sm, tp, hedge, vrp_min=vrp)
        if df.empty:
            continue
        IS = df[df.entry < "2024-01-01"]["rs"]
        rows.append({"dte": dte_e, "wmult": wm, "stop": sm, "tp": tp, "hedge": hedge, "vrp": vrp,
                     **{f"IS_{k}": v for k, v in stats(IS).items()}})
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/research_strangle_IS.csv", index=False)
    pd.set_option("display.width", 250)
    print(res.sort_values("IS_sr_w", ascending=False).head(30).to_string())
    print("\nRobustness: mean IS weekly Sharpe by each parameter value")
    for p in ["dte", "wmult", "stop", "tp", "hedge", "vrp"]:
        print(p, res.groupby(res[p].astype(str))["IS_sr_w"].mean().round(2).to_dict())
