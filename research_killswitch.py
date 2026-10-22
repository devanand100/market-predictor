"""
Kill-switch rules for Strategy 6, backtested on the shadow record (the full strategy always trades, as the paper books
do). Before each new trade a rule looks only at shadow trades already closed, the live equity reached so far, or the
previous session's VRP, and decides paused or active. The "live" result compounds only the trades taken while active
(each trade's return on the equity at its entry, so sizing follows the shadow book), and running costs are always paid.

Shadow records (Strategy 6, Rs 10L, research weights):
  chained       synthetic 2019-11..2020-12 (research_synthetic_options.py base) + real 2021-01..2026-09
  pessimistic   synthetic with IV x0.85 above VIX 21 (the harsh COVID case) + real 2021+
  real          real 2021+ only
Max drawdown here is on the trade-close equity curve (open losses not marked), so compare rules with each other.

  python research_killswitch.py
Writes data/reports/killswitch_results.csv
"""
import json
import sys

import numpy as np
import pandas as pd

import research_realtrade as rt

REP = "data/reports/"
START = 1_000_000


def with_returns(T, E, start=START):
    """Per-trade return on the equity at the session before entry; running costs as their own rows."""
    E = E.sort_values("date")
    eq = pd.Series(E.equity.to_numpy(), index=E.date)
    prev = eq.shift(1).fillna(start)
    T = T.copy()
    T["eq0"] = T.entry.map(lambda d: prev.get(d, prev[prev.index <= d].iloc[-1] if (prev.index <= d).any() else start))
    T["ret"] = T.pnl / T.eq0
    return T[["sleeve", "entry", "exit", "pnl", "ret"]]


def shadow(name):
    real = with_returns(pd.read_csv(REP + "realtrade_strategy_trades.csv"), pd.read_csv(REP + "realtrade_strategy_equity.csv"))
    if name == "real":
        return real
    if name == "chained":
        syn = with_returns(pd.read_csv(REP + "synthetic_2019_20_trades.csv"), pd.read_csv(REP + "synthetic_2019_20_equity.csv"))
    else:
        import research_synthetic_options as so
        m = so.Model()
        m.hi_mult = 0.85
        daily = so.daily_frame()
        E, T, _ = so.run(so.synth_days(so.trading_dates_pre2021(), daily, m), daily, "2019-11-18", "2020-12-31")
        syn = with_returns(T, E)
    return pd.concat([syn, real], ignore_index=True)


def pf(x):
    x = np.asarray(x, float)
    l = -x[x < 0].sum()
    return x[x > 0].sum() / l if l > 0 else np.inf


def vrp_avg20():
    _, daily = rt.data()
    v = (daily["vix_prev"] - daily["rv20"].shift(1)).rolling(20, min_periods=10).mean()
    return {d.strftime("%Y-%m-%d"): x for d, x in v.items()}


class Rule:
    """Hysteresis rule: pause when `bad(ctx)`, resume when `good(ctx)`; scope 'all', 'shorts' or 'sleeve'."""

    def __init__(self, name, bad=None, good=None, scope="all"):
        self.name, self.bad, self.good, self.scope = name, bad, good, scope
        self.paused = {}

    def allows(self, sleeve, ctx):
        key = sleeve if self.scope == "sleeve" else "all"
        if self.scope == "shorts" and sleeve == "GAP":
            return True
        c = dict(ctx, sleeve=sleeve)
        if self.paused.get(key):
            if self.good(c):
                self.paused[key] = False
        elif self.bad(c):
            self.paused[key] = True
        return not self.paused.get(key)


def rolling(ctx, n, sleeve=None):
    x = [p for s, p in ctx["closed"] if sleeve is None or s == sleeve][-n:]
    return pf(x) if len(x) >= n else None


def rules():
    lt = lambda v, t: v is not None and v < t
    ge = lambda v, t: v is None or v >= t
    R = [Rule("none", lambda c: False, lambda c: True)]
    R.append(Rule("edge 40 < 0.9 / resume 1.3", lambda c: lt(rolling(c, 40), 0.9), lambda c: ge(rolling(c, 40), 1.3)))
    R.append(Rule("edge 40 < 1.0 / resume 1.2", lambda c: lt(rolling(c, 40), 1.0), lambda c: ge(rolling(c, 40), 1.2)))
    R.append(Rule("20 trades < 0.8 / resume 1.2", lambda c: lt(rolling(c, 20), 0.8), lambda c: ge(rolling(c, 20), 1.2)))
    R.append(Rule("sleeve 15 < 0.5 / resume 1.2", lambda c: lt(rolling(c, 15, c["sleeve"]), 0.5),
                  lambda c: ge(rolling(c, 15, c["sleeve"]), 1.2), scope="sleeve"))
    for dd in (-0.20, -0.15):
        R.append(Rule(f"drawdown {dd:.0%} / resume 20-PF 1.3", (lambda t: lambda c: c["dd"] <= t)(dd),
                      lambda c: ge(rolling(c, 20), 1.3)))
    R.append(Rule("VRP 20d < 0 (strangles)", lambda c: c["vrp"] == c["vrp"] and c["vrp"] < 0,
                  lambda c: not (c["vrp"] == c["vrp"]) or c["vrp"] >= 0, scope="shorts"))
    R.append(Rule("edge 40 < 0.9 or drawdown -20%",
                  lambda c: lt(rolling(c, 40), 0.9) or c["dd"] <= -0.20,
                  lambda c: ge(rolling(c, 40), 1.3) and ge(rolling(c, 20), 1.3)))
    return R


def simulate(T, rule, vrp):
    T = T.sort_values(["entry", "exit"]).reset_index(drop=True)
    trades = T[T.sleeve != "running"]
    run_costs = T[T.sleeve == "running"]
    taken, skipped = [], 0
    closed = []                                     # (exit, sleeve, ret) of shadow trades
    for r in trades.itertuples():
        done = sorted([c for c in closed if c[0] < r.entry])
        live = 1.0
        peak = 1.0
        # live equity at entry: realised taken trades + running costs before entry, in exit order
        ev = sorted([(t[0], t[2]) for t in taken if t[0] < r.entry] +
                    [(x.exit, x.ret) for x in run_costs.itertuples() if x.exit < r.entry])
        for _, ret in ev:
            live *= 1 + ret
            peak = max(peak, live)
        ctx = dict(closed=[(s, p) for _, s, p in done], dd=live / peak - 1, vrp=vrp.get(r.entry, np.nan))
        if rule.allows(r.sleeve, ctx):
            taken.append((r.exit, r.sleeve, r.ret))
        else:
            skipped += 1
        closed.append((r.exit, r.sleeve, r.ret))
    ev = sorted([(t[0], t[2], t[1]) for t in taken] + [(x.exit, x.ret, "running") for x in run_costs.itertuples()])
    eq = np.cumprod([1 + x[1] for x in ev])
    dates = [x[0] for x in ev]
    yrs = (pd.Timestamp(dates[-1]) - pd.Timestamp(T.entry.min())).days / 365.25
    dd = (eq / np.maximum.accumulate(np.concatenate([[1], eq]))[1:] - 1).min()
    rets = [t[2] for t in taken]
    oos = [t[2] for t in taken if t[0] >= "2024-01-01"]
    by_year = {}
    prev = 1.0
    for y in sorted({d[:4] for d in dates}):
        last = [e for d, e in zip(dates, eq) if d[:4] == y][-1]
        by_year[y] = round(100 * (last / prev - 1), 1)
        prev = last
    return dict(rule=rule.name, cagr=round(100 * (eq[-1] ** (1 / yrs) - 1), 1), max_dd=round(100 * dd, 1),
                pf=round(pf(rets), 2), oos_pf=round(pf(oos), 2), trades=len(taken), skipped=skipped,
                years=json.dumps(by_year))


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    vrp = vrp_avg20()
    rows = []
    for name in ("chained", "pessimistic", "real"):
        T = shadow(name)
        for rule in rules():
            r = dict(shadow=name, **simulate(T, rule, vrp))
            rows.append(r)
            print(f"{name:12s} {r['rule']:34s} CAGR {r['cagr']:6.1f} DD {r['max_dd']:6.1f} PF {r['pf']:5.2f} "
                  f"OOS PF {r['oos_pf']:5.2f} taken {r['trades']:4d} skipped {r['skipped']:4d} {r['years']}", flush=True)
    pd.DataFrame(rows).to_csv(REP + "killswitch_results.csv", index=False)
