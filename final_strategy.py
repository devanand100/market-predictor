"""
FINAL STRATEGY — "Weekly Expected-Move Strangle" (Nifty weekly options)

Rules (chosen on 2021-2023 only; 2024-2026 is untouched out-of-sample):
  Entry   : 09:30 exactly 4 sessions before expiry (dte_entry=4). Weeks with < 5 sessions are
            skipped (holiday weeks) - this skipped the Apr-2025 tariff-crash week, see research_institutional.py.
  Strikes : ATM straddle premium S at 09:30. Sell CE at ATM + S, sell PE at ATM - S
            (rounded to 50). i.e. short strikes at the market-implied 1-sigma weekly move.
  Stop    : exit BOTH legs if position loss >= 1.5 x credit received (checked every minute).
  Exit    : otherwise hold to expiry day 15:15 (never held into settlement).
  Sizing  : lots of 65 units; margin ~9% of notional per strangle; use at most 60% of equity
            as margin (40% buffer for MTM swings / exchange margin hikes). Compounding.
Costs: brokerage, STT (date-aware), exchange, SEBI, stamp, GST, slippage (0.3pt+0.5% normal,
0.5pt+1.5% on stops), plus Rs 1,990/month fixed running costs.
"""
import json
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily
from research_strangle import backtest
from cost_model import MONTHLY_RUNNING_COSTS
import cost_model

LOT = 65
MARGIN_PCT = 0.09
MAX_MARGIN_USE = 0.60
START_CAPITAL = 500_000
RUNNING_PER_MONTH = sum(MONTHLY_RUNNING_COSTS.values())
PARAMS = dict(dte_entry=4, width_mult=1.0, stop_mult=1.5, tp=None, hedge=False, vrp_min=None)


def trades(slip_mult=1.0):
    orig = cost_model.slip

    def scaled(p, urgent=False):
        return orig(p, urgent) * slip_mult
    rp.slip = scaled
    rp.QTY = LOT
    days, daily = load_days(), load_daily()
    df = backtest(days, daily, rp.cycles(daily, days), **PARAMS)
    rp.slip = orig
    return df, daily


def simulate(df, start=START_CAPITAL, running=True):
    eq = start
    rows = []
    last_month = None
    for r in df.itertuples():
        month = r.entry[:7]
        fixed = 0.0
        if running and month != last_month:
            fixed = RUNNING_PER_MONTH
            last_month = month
        margin_lot = MARGIN_PCT * r.spot * LOT
        lots = int(MAX_MARGIN_USE * eq // margin_lot)
        pnl = r.rs * lots - fixed          # r.rs is per-lot net of statutory costs & slippage
        eq_before = eq
        eq += pnl
        rows.append({"entry": r.entry, "exit": r.exit, "reason": r.reason, "lots": lots,
                     "spot": round(r.spot, 1), "vix": round(r.vix, 2) if r.vix == r.vix else None,
                     "credit_pts": round(r.credit, 1), "pnl_pts": round(r.pts, 1),
                     "stat_costs": round(r.cost * lots), "fixed_costs": fixed, "pnl": round(pnl),
                     "ret_pct": pnl / eq_before * 100, "equity": round(eq)})
    return pd.DataFrame(rows)


def metrics(led, start=START_CAPITAL):
    t0 = pd.Timestamp(led["entry"].iloc[0])
    t1 = pd.Timestamp(led["exit"].iloc[-1])
    yrs = (t1 - t0).days / 365.25
    eq = pd.concat([pd.Series([start]), led["equity"]]).reset_index(drop=True)
    dd = eq / eq.cummax() - 1
    r = led["ret_pct"] / 100
    wins, losses = led[led.pnl > 0], led[led.pnl <= 0]
    return {
        "start": str(t0.date()), "end": str(t1.date()), "years": round(yrs, 2),
        "trades": len(led), "final_equity": int(eq.iloc[-1]),
        "cagr_pct": round(((eq.iloc[-1] / start) ** (1 / yrs) - 1) * 100, 1),
        "max_dd_pct": round(dd.min() * 100, 1),
        "sharpe_weekly_ann": round(r.mean() / r.std() * np.sqrt(52), 2),
        "sortino": round(r.mean() / r[r < 0].std() * np.sqrt(52), 2),
        "win_rate_pct": round(len(wins) / len(led) * 100, 1),
        "avg_win_pct": round(wins.ret_pct.mean(), 2), "avg_loss_pct": round(losses.ret_pct.mean(), 2),
        "profit_factor": round(wins.pnl.sum() / -losses.pnl.sum(), 2),
        "worst_week_pct": round(led.ret_pct.min(), 2), "best_week_pct": round(led.ret_pct.max(), 2),
        "stops_hit": int((led.reason == "stop").sum()),
        "statutory_costs": int(led.stat_costs.sum()), "fixed_running_costs": int(led.fixed_costs.sum()),
    }


def monte_carlo(led, n=10000, weeks=52 * 5, seed=7):
    rng = np.random.default_rng(seed)
    r = led["ret_pct"].to_numpy() / 100
    paths = rng.choice(r, size=(n, weeks), replace=True)
    eq = np.cumprod(1 + paths, axis=1)
    cagr = (eq[:, -1] ** (52 / weeks) - 1) * 100
    dd = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(1) * 100
    pct = lambda a, q: round(float(np.percentile(a, q)), 1)
    return {"paths": n, "horizon_years": weeks / 52,
            "cagr_p5": pct(cagr, 5), "cagr_p50": pct(cagr, 50), "cagr_p95": pct(cagr, 95),
            "maxdd_p50": pct(dd, 50), "maxdd_p95_worst": pct(dd, 5), "maxdd_p99_worst": pct(dd, 1),
            "prob_loss_over_horizon_pct": round(float((eq[:, -1] < 1).mean() * 100), 2)}


if __name__ == "__main__":
    out = {"params": PARAMS, "capital": START_CAPITAL, "running_costs_per_month": MONTHLY_RUNNING_COSTS}
    df, daily = trades(1.0)
    led = simulate(df)
    led.to_csv("data/reports/final_trades.csv", index=False)
    out["full"] = metrics(led)
    out["in_sample"] = metrics(simulate(df[df.entry < "2024-01-01"]))
    out["out_of_sample"] = metrics(simulate(df[df.entry >= "2024-01-01"]))
    led["yr"] = led.entry.str[:4]
    out["yearly"] = {y: {"trades": len(g), "return_pct": round((g.equity.iloc[-1] / (g.equity.iloc[0] - g.pnl.iloc[0]) - 1) * 100, 1),
                         "win_rate": round((g.pnl > 0).mean() * 100, 1)} for y, g in led.groupby("yr")}
    out["monte_carlo"] = monte_carlo(led)
    df2, _ = trades(2.0)
    out["stress_double_slippage"] = metrics(simulate(df2))
    out["no_running_costs"] = metrics(simulate(df, running=False))
    out["worst_trades"] = led.nsmallest(8, "ret_pct")[["entry", "exit", "reason", "vix", "ret_pct"]].round(2).to_dict("records")
    # Benchmark: Nifty buy & hold over the same window
    a, b = pd.Timestamp(out["full"]["start"]), pd.Timestamp(out["full"]["end"])
    px = daily.loc[a:b, "close"]
    out["benchmark_nifty_cagr_pct"] = round(((px.iloc[-1] / px.iloc[0]) ** (365.25 / (b - a).days) - 1) * 100, 1)
    nd = (px / px.cummax() - 1).min() * 100
    out["benchmark_nifty_maxdd_pct"] = round(nd, 1)
    # Monthly return table for the report
    led["month"] = led.exit.str[:7]
    m = led.groupby("month").apply(lambda g: (g.equity.iloc[-1] / (g.equity.iloc[0] - g.pnl.iloc[0]) - 1) * 100)
    out["monthly_returns"] = m.round(2).to_dict()
    out["equity_curve"] = led[["exit", "equity"]].values.tolist()
    with open("data/reports/final_strategy_summary.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    for k in ["full", "in_sample", "out_of_sample", "stress_double_slippage", "no_running_costs", "monte_carlo",
              "yearly", "worst_trades"]:
        print(k, json.dumps(out[k], default=str))
    print("benchmark", out["benchmark_nifty_cagr_pct"], out["benchmark_nifty_maxdd_pct"])
