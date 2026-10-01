"""
STRATEGY 2 — "Confirmed Directional Credit Spread" (Nifty weekly options)

Direction (both must agree, else no trade that week):
  1. FII index-options positioning: day-over-day change in FII net option delta
     (CE long - CE short) - (PE long - PE short) from the NSE participant OI file published the
     evening before entry, minus the median of past weekly values. > 0 bullish, < 0 bearish.
  2. Opening gap: Nifty at 09:30 on entry day vs previous close. Up = bullish, down = bearish.
Trade (09:30, first session after expiry):
  bullish -> sell PE at ATM - 0.5 x straddle, buy PE 200 points lower (bull put spread)
  bearish -> sell CE at ATM + 0.5 x straddle, buy CE 200 points higher (bear call spread)
  No stop (risk is defined by the spread). Close at 15:15 on expiry day.
Sizing: lots of 65; a full max-loss week (200 pts x lots x 65) is capped at 8% of equity.
Also runs a 50/50 capital split with Strategy 1 (weekly strangle).
"""
import json
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily
from research_directional_spreads import make_legs_fn, signal_table, SPREAD_W
from cost_model import MONTHLY_RUNNING_COSTS
import final_strategy as fs

LOT = 65
RISK_PER_WEEK = 0.08
START = 500_000
RUNNING = sum(MONTHLY_RUNNING_COSTS.values())


def spread_trades():
    rp.QTY = LOT
    days, daily = load_days(), load_daily()
    # Signals are always recomputed from source data (never read from a possibly stale CSV)
    sig = signal_table(daily, days, rp.cycles(daily, days))
    sig.to_csv("data/reports/weekly_signals.csv")
    g, f = np.sign(sig["pa_gap"]), np.sign(sig["fii_opt_chg"])
    direction = g.where((g == f) & (f != 0), 0)
    rows = []
    for window, exp in rp.cycles(daily, days):
        e = window[0]
        d = direction.get(e, 0)
        if not d or np.isnan(d):
            continue
        r = rp.run_cycle(days, window, make_legs_fn(days[e], int(d), "credit"), None, None)
        if r:
            r["dir"] = "bull put" if d > 0 else "bear call"
            rows.append(r)
    return pd.DataFrame(rows), daily


def simulate_spreads(df, start=START, running=True):
    eq, rows, last_m = start, [], None
    for r in df.itertuples():
        fixed = 0.0
        if running and r.entry[:7] != last_m:
            fixed, last_m = RUNNING, r.entry[:7]
        lots = max(1, int(RISK_PER_WEEK * eq // (SPREAD_W * LOT)))
        pnl = r.rs * lots - fixed
        before = eq
        eq += pnl
        rows.append({"entry": r.entry, "exit": r.exit, "reason": r.reason, "dir": r.dir, "lots": lots,
                     "spot": round(r.spot, 1), "credit_pts": round(r.credit, 1), "pnl_pts": round(r.pts, 1),
                     "stat_costs": round(r.cost * lots), "fixed_costs": fixed, "pnl": round(pnl),
                     "ret_pct": pnl / before * 100, "equity": round(eq), "vix": np.nan})
    return pd.DataFrame(rows)


def monte_carlo_trades(led, years=5, n=10000, seed=7):
    """Bootstrap trades (not calendar weeks): draw trades_per_year x years trades per path."""
    span = (pd.Timestamp(led.exit.iloc[-1]) - pd.Timestamp(led.entry.iloc[0])).days / 365.25
    k = int(round(len(led) / span * years))
    rng = np.random.default_rng(seed)
    eq = np.cumprod(1 + rng.choice(led.ret_pct.to_numpy() / 100, (n, k)), 1)
    cagr = (eq[:, -1] ** (1 / years) - 1) * 100
    dd = (eq / np.maximum.accumulate(eq, 1) - 1).min(1) * 100
    q = lambda a, p: round(float(np.percentile(a, p)), 1)
    return {"paths": n, "horizon_years": years, "trades_per_year": round(len(led) / span, 1),
            "cagr_p5": q(cagr, 5), "cagr_p50": q(cagr, 50), "cagr_p95": q(cagr, 95),
            "maxdd_p50": q(dd, 50), "maxdd_p95_worst": q(dd, 5), "maxdd_p99_worst": q(dd, 1),
            "prob_loss_over_horizon_pct": round(float((eq[:, -1] < 1).mean() * 100), 2)}


def combine(led_a, led_b, start=START):
    """50/50 capital split, each sleeve compounding on its own half; running costs paid once."""
    a = led_a.set_index("exit")["ret_pct"] / 100
    b = led_b.set_index("exit")["ret_pct"] / 100
    idx = sorted(set(a.index) | set(b.index))
    ea = eb = start / 2
    rows = []
    for d in idx:
        before = ea + eb
        ea *= 1 + a.get(d, 0.0)
        eb *= 1 + b.get(d, 0.0)
        rows.append({"entry": d, "exit": d, "reason": "", "pnl": ea + eb - before,
                     "ret_pct": (ea + eb) / before * 100 - 100, "equity": round(ea + eb),
                     "stat_costs": 0, "fixed_costs": 0})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    df, daily = spread_trades()
    led = simulate_spreads(df)
    led.to_csv("data/reports/spread_strategy_trades.csv", index=False)
    out = {"full": fs.metrics(led),
           "in_sample_2022_23": fs.metrics(simulate_spreads(df[df.entry < "2024-01-01"])),
           "out_of_sample_2024_26": fs.metrics(simulate_spreads(df[df.entry >= "2024-01-01"])),
           "no_running_costs": fs.metrics(simulate_spreads(df, running=False)),
           "monte_carlo": monte_carlo_trades(led)}
    led["yr"] = led.entry.str[:4]
    out["yearly"] = {y: {"trades": len(g), "return_pct": round((g.equity.iloc[-1] / (g.equity.iloc[0] - g.pnl.iloc[0]) - 1) * 100, 1),
                         "win_rate": round((g.pnl > 0).mean() * 100, 1)} for y, g in led.groupby("yr")}
    out["direction_split"] = led.groupby("dir").agg(n=("pnl", "size"), win=("pnl", lambda s: round((s > 0).mean() * 100, 1)),
                                                     avg_ret=("ret_pct", lambda s: round(s.mean(), 2))).to_dict("index")
    out["avg_lots"] = round(led.lots.mean(), 1)
    out["avg_credit_pts"] = round(led.credit_pts.mean(), 1)
    out["worst_trades"] = led.nsmallest(6, "ret_pct")[["entry", "exit", "dir", "ret_pct"]].round(2).to_dict("records")

    # Portfolio with Strategy 1 over the common period (2022+), fixed costs charged once
    s1 = pd.read_csv("data/reports/final_trades.csv")
    s1 = s1[s1.entry >= "2022-01-01"]
    s1_noc = fs.simulate(fs.trades(1.0)[0].query("entry >= '2022-01-01'"), running=False)
    s2_noc = simulate_spreads(df, running=False)
    j = s1_noc.set_index("exit")["ret_pct"].to_frame("s1").join(s2_noc.set_index("exit")["ret_pct"].rename("s2"), how="outer").fillna(0)
    out["weekly_return_correlation"] = round(j.s1.corr(j.s2), 2)
    port = combine(s1_noc, s2_noc)
    # charge running costs once per month
    eq, last_m, rows = START, None, []
    for r in port.itertuples():
        fixed = RUNNING if r.exit[:7] != last_m else 0
        last_m = r.exit[:7]
        before = eq
        eq = eq * (1 + r.ret_pct / 100) - fixed
        rows.append({"entry": r.exit, "exit": r.exit, "reason": "", "pnl": eq - before, "ret_pct": (eq / before - 1) * 100,
                     "equity": round(eq), "stat_costs": 0, "fixed_costs": fixed})
    port = pd.DataFrame(rows)
    out["portfolio_50_50"] = fs.metrics(port)
    s1_only = fs.metrics(fs.simulate(fs.trades(1.0)[0].query("entry >= '2022-01-01'")))
    out["strangle_only_same_period"] = s1_only
    port.to_csv("data/reports/portfolio_equity.csv", index=False)
    with open("data/reports/spread_strategy_summary.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    for k, v in out.items():
        print(k, json.dumps(v, default=str))
