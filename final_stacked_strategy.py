"""
Strategy 4: stacked strangle + low-VIX gap buy (research: research_regime_switch.py).

Every full week (09:30, 4 sessions before expiry), in one account:
  * Strategy 1 strangle: sell CE/PE at ATM +/- ATM straddle, exit both at 1.5x credit loss,
    else 15:15 expiry day. Lots = floor(STRANGLE_MARGIN x equity / (9% x spot x 65)).
  * Strategy 3 gap buy, only if VIX(09:20) <= 15: ATM CE (gap up) / PE (gap down), limit sell
    at 2x, else 15:15 expiry day; GAP_FRAC of equity in premium (1 lot allowed up to 2x).
The two are slightly negatively correlated (-0.17 weekly), so stacking adds return.

Sizes chosen on 2021-23 with fractional lots (`data/reports/stack_grid_fractional.csv`):
highest IS CAGR with IS max DD better than -22% -> STRANGLE_MARGIN 0.30, GAP_FRAC 0.03.
One strangle lot needs ~Rs 1.46L margin, so 30% sizing needs >= ~Rs 5-6L; below that the
script uses MIN_ONE_LOT (1 strangle lot if margin <= 55% of equity).

The strangle's position-level stop needs automation, so the default charges Rs 1,990/month.
Writes data/reports/stacked_strategy_summary.json, stacked_strategy_trades.csv
"""
import json
import numpy as np
import pandas as pd

import research_regime_switch as R

STRANGLE_MARGIN = 0.30
GAP_FRAC = 0.03
VIX_MAX = 15
MIN_ONE_LOT = 0.55
IS_END = "2023-12-31"


def simulate(wk, s1, s3, start, running=True, use=STRANGLE_MARGIN, frac=GAP_FRAC):
    eq, last_m, rows = float(start), None, []
    for e, w in wk.iterrows():
        pnl, what = 0.0, []
        if e in s1.index:
            r = s1.loc[e]
            m_lot = R.fs.MARGIN_PCT * r.spot * R.LOT
            lots = int(use * eq // m_lot) or (1 if m_lot <= MIN_ONE_LOT * eq else 0)
            pnl += r.rs * lots
            what.append(f"strangle x{lots}")
        if e in s3.index and w.vix <= VIX_MAX:
            p, n = R.gap_pnl(s3.loc[e], eq, frac)
            pnl += p
            what.append(f"gap {s3.at[e, 'typ']} x{n}")
        fixed = 0.0
        if running and e[:7] != last_m:
            fixed, last_m = R.RUNNING, e[:7]
        before = eq
        eq += pnl - fixed
        rows.append(dict(entry=e, vix=round(w.vix, 2), trade=" + ".join(what), pnl=round(pnl - fixed),
                         ret=(pnl - fixed) / before, equity=round(eq)))
    return pd.DataFrame(rows)


def by_year(led, start):
    out, prev = {}, start
    for y, g in led.groupby(led.entry.str[:4]):
        out[y] = round(100 * (g.equity.iloc[-1] / prev - 1), 1)
        prev = g.equity.iloc[-1]
    return out


def monte_carlo(led, years=5, n=10000, seed=7):
    r = led.ret.to_numpy()
    k = int(round(len(r) / ((pd.Timestamp(led.entry.iloc[-1]) - pd.Timestamp(led.entry.iloc[0])).days / 365.25) * years))
    P = np.cumprod(1 + np.random.default_rng(seed).choice(r, (n, k)), 1)
    dd = (P / np.maximum.accumulate(P, 1) - 1).min(1)
    cagr = P[:, -1] ** (1 / years) - 1
    return dict(median_cagr=round(100 * np.median(cagr), 1), p5_cagr=round(100 * np.percentile(cagr, 5), 1),
                median_dd=round(100 * np.median(dd), 1), dd_1_in_20=round(100 * np.percentile(dd, 5), 1),
                p_loss=round(100 * (P[:, -1] < 1).mean(), 1))


if __name__ == "__main__":
    wk, s1, s3 = R.week_table()
    summary = {}
    for start in [300_000, 600_000, 1_000_000]:
        for running in [False, True]:
            R.START = start
            led = simulate(wk, s1, s3, start, running)
            ev = R.evaluate(led)
            key = f"start={start} running_costs={running}"
            summary[key] = dict(**ev, years=by_year(led, start), mc=monte_carlo(led), final=int(led.equity.iloc[-1]),
                                avg_strangle_lots=round(led.trade.str.extract(r"strangle x(\d+)")[0].astype(float).mean(), 1))
            if start == 600_000 and running:
                led.to_csv("data/reports/stacked_strategy_trades.csv", index=False)
            s, m = summary[key], summary[key]["mc"]
            print(f"{key:36s} CAGR {s['all_cagr']:5.1f} DD {s['all_dd']:6.1f} Sh {s['all_sharpe']} worst wk {s['all_worst']} "
                  f"| IS {s['is_cagr']:5.1f}/{s['is_dd']:6.1f} OOS {s['oos_cagr']:5.1f}/{s['oos_dd']:6.1f} "
                  f"| MC med {m['median_cagr']} p5 {m['p5_cagr']} DD med {m['median_dd']} 1in20 {m['dd_1_in_20']} "
                  f"| lots {s['avg_strangle_lots']} | {s['years']}")
    with open("data/reports/stacked_strategy_summary.json", "w") as fh:
        json.dump(summary, fh, indent=1)
