"""
Strategy 3: weekly gap-direction option BUY (research: research_option_buying_positional.py).

Rules
  * Trade only full weeks: enter 4 sessions before expiry at 09:30 (minute 15).
  * Direction = sign of the opening gap: spot at 09:30 vs the previous session's close.
    Gap up -> buy the ATM CE, gap down -> buy the ATM PE, of the nearest weekly expiry.
  * No stop (max loss = premium paid). Take profit with a resting limit sell at 2x the fill
    (TP = 1.0); otherwise sell at 15:15 on expiry day. Never held into settlement.
  * Sizing: spend FRAC of equity on premium, whole lots of 65; skip the week if one lot costs
    more than 2 x FRAC of equity.
Manual execution is possible (one order on Monday/entry day + one GTT/limit target), so the
default runs with NO fixed monthly costs; `running=True` adds the Rs 1,990/month algo setup.

Writes data/reports/gap_buy_trades.csv, gap_buy_summary.json
"""
import json
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily, atm_strike
from cost_model import option_fill_cost, MONTHLY_RUNNING_COSTS

LOT = 65
START = 300_000
RUNNING = sum(MONTHLY_RUNNING_COSTS.values())
IS_END = "2023-12-31"


def trades(tp=1.0):
    rp.QTY = LOT
    days, daily = load_days(), load_daily()
    closes = daily["close"]
    rows = []
    for window, exp in rp.cycles(daily, days):
        if len(window) < 5:
            continue
        w = window[-5:]
        d0 = days[w[0]]
        if np.isnan(d0.ce[rp.ENTRY_MIN]).all():      # special session without 09:30 prices
            continue
        ts = pd.Timestamp(w[0])
        prev_close = closes.iloc[daily.index.get_loc(ts) - 1]
        gap = d0.spot[rp.ENTRY_MIN] / prev_close - 1
        if gap == 0:
            continue
        typ = "CE" if gap > 0 else "PE"
        r = rp.run_cycle(days, w, lambda s: [(typ, atm_strike(s), 1)], None, tp)
        if not r:
            continue
        debit = -r["credit"]
        rows.append(dict(entry=w[0], expiry=exp, exit=r["exit"], typ=typ, gap=gap,
                         vix=daily.loc[ts, "vix_0920"], reason=r["reason"],
                         buy=debit, sell=debit + r["pts"]))
    return pd.DataFrame(rows)


def simulate(T, frac, start=START, running=False, vix_max=None):
    eq, last_m, out = start, None, []
    for r in T.itertuples():
        fixed = 0.0
        if running and r.entry[:7] != last_m:
            fixed, last_m = RUNNING, r.entry[:7]
        pnl, lots = 0.0, 0
        if vix_max is None or r.vix <= vix_max:
            per_lot = r.buy * LOT
            lots = int(frac * eq // per_lot)
            if lots == 0 and per_lot <= 2 * frac * eq:
                lots = 1
            if lots:
                q = lots * LOT
                pnl = (r.sell - r.buy) * q - option_fill_cost(r.buy, q, True, r.entry) \
                    - option_fill_cost(r.sell, q, False, r.exit)
        eq += pnl - fixed
        out.append(dict(entry=r.entry, lots=lots, pnl=pnl, equity=eq, ret=(pnl - fixed) / (eq - pnl + fixed)))
    return pd.DataFrame(out)


def metrics(led, start=START, d0=None, d1=None):
    e = np.concatenate([[start], led["equity"].to_numpy()])
    t0, t1 = pd.Timestamp(d0 or led["entry"].iloc[0]), pd.Timestamp(d1 or led["entry"].iloc[-1])
    yrs = (t1 - t0).days / 365.25 + 7 / 365.25
    traded = led[led["lots"] > 0]
    wk = led["ret"]
    return dict(cagr=round(100 * ((e[-1] / start) ** (1 / yrs) - 1), 1),
                max_dd=round(100 * (e / np.maximum.accumulate(e) - 1).min(), 1),
                sharpe=round(wk.mean() / wk.std() * np.sqrt(52), 2) if wk.std() > 0 else None,
                trades=len(traded), win=round(100 * (traded["pnl"] > 0).mean(), 1),
                worst_week=round(100 * wk.min(), 1), best_week=round(100 * wk.max(), 1),
                final=int(e[-1]))


def by_year(led, start=START):
    led = led.copy()
    led["y"] = led["entry"].str[:4]
    out, prev = {}, start
    for y, g in led.groupby("y"):
        out[y] = round(100 * (g["equity"].iloc[-1] / prev - 1), 1)
        prev = g["equity"].iloc[-1]
    return out


def monte_carlo(T, frac, vix_max=None, years=5, n=10000, seed=7):
    """Bootstrap net per-trade returns on premium (costs at 2 lots), ~38 trades a year."""
    s = T if vix_max is None else T[T.vix <= vix_max]
    q = 2 * LOT
    r = np.array([((x.sell - x.buy) * q - option_fill_cost(x.buy, q, True, x.entry)
                   - option_fill_cost(x.sell, q, False, x.exit)) / (x.buy * q) for x in s.itertuples()])
    per_year = len(s) / ((pd.Timestamp(T.entry.iloc[-1]) - pd.Timestamp(T.entry.iloc[0])).days / 365.25)
    k = int(round(per_year * years))
    rng = np.random.default_rng(seed)
    paths = np.cumprod(1 + frac * rng.choice(r, (n, k)), axis=1)
    cagr = paths[:, -1] ** (1 / years) - 1
    dd = (paths / np.maximum.accumulate(paths, axis=1) - 1).min(1)
    return dict(median_cagr=round(100 * np.median(cagr), 1), p5_cagr=round(100 * np.percentile(cagr, 5), 1),
                median_dd=round(100 * np.median(dd), 1), p95_dd=round(100 * np.percentile(dd, 5), 1),
                p_loss=round(100 * (paths[:, -1] < 1).mean(), 1))


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    summary = {}
    for tp in [1.0, None]:
        T = trades(tp)
        if tp == 1.0:
            T.to_csv("data/reports/gap_buy_trades.csv", index=False)
        for vix_max in [None, 15]:
            for frac in [0.03, 0.05, 0.075, 0.10]:
                led = simulate(T, frac, vix_max=vix_max)
                key = f"tp={tp} vix<={vix_max} frac={frac}"
                row = dict(full=metrics(led),
                           IS=metrics(led[led.entry <= IS_END], d1=IS_END),
                           OOS=metrics(led[led.entry > IS_END], start=led[led.entry <= IS_END]["equity"].iloc[-1]),
                           with_running_costs=metrics(simulate(T, frac, vix_max=vix_max, running=True)),
                           years=by_year(led), mc=monte_carlo(T, frac, vix_max))
                summary[key] = row
                f, i, o, m = row["full"], row["IS"], row["OOS"], row["mc"]
                print(f"{key:32s} CAGR {f['cagr']:5.1f} DD {f['max_dd']:6.1f} Sh {f['sharpe']} n {f['trades']} "
                      f"win {f['win']} | IS {i['cagr']:5.1f}/{i['max_dd']:6.1f} OOS {o['cagr']:5.1f}/{o['max_dd']:6.1f} "
                      f"| algo-cost CAGR {row['with_running_costs']['cagr']:5.1f} | MC med {m['median_cagr']} p5 {m['p5_cagr']} "
                      f"ddmed {m['median_dd']} dd95 {m['p95_dd']} Ploss {m['p_loss']} | {row['years']}")
    with open("data/reports/gap_buy_summary.json", "w") as fh:
        json.dump(summary, fh, indent=1)
