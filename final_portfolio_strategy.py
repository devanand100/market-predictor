"""
Strategy 5: three-sleeve portfolio (research: research_portfolio_sleeves.py).

Sleeves, one account, sizes chosen on 2021-23 (highest IS CAGR with IS max DD better than -12%):
  L2   weekly time-staggered strangle, 30% of equity as margin. Each of the first 3 sessions after
       expiry, 09:30, sell 1/3 of the lots as a strangle at ATM +/- straddle; each tranche exits at
       1.5x its credit; else 15:15 expiry day. (research_institutional.py, kind "L2")
  GAP  Strategy 3 gap buy on full weeks with VIX(09:20) <= 15, 3% of equity in premium.
  MON  monthly short ATM straddle ~45 DTE -> 21 DTE, only when VIX - RV20 >= 2 (prev session),
       20% of equity as margin, no stop. (research_monthly_straddle*.py, 60-min data)
Total planned margin 50%.

Event-driven and lot-exact: a trade is sized on realised equity at its entry and booked at its
exit. Per-lot P&L: L2 = 10-lot result / 10 (it must be >= 3 lots to stagger, otherwise skipped),
MON = per-lot net Rs, GAP = exact costs at the traded quantity. Monthly straddles also book their
worst open loss mid-trade (reversed at exit) so drawdowns include it. Rs 1,990/month running costs
(L2 tranche stops need an algo).

Writes data/reports/portfolio_strategy_summary.json, portfolio_strategy_trades.csv
"""
import json
import numpy as np
import pandas as pd

import final_gap_buy as gb
from cost_model import MONTHLY_RUNNING_COSTS
from research_regime_switch import gap_pnl

LOT = 65
MARGIN_PCT = 0.09
W_L2, W_GAP, W_MON = 0.30, 0.03, 0.20
VIX_MAX, VRP_MIN = 15, 2
RUNNING = sum(MONTHLY_RUNNING_COSTS.values())
IS_END = "2023-12-31"


def events():
    ev = []
    inst = pd.read_csv("data/reports/research_institutional_trades.csv")
    for r in inst[inst.strategy == "L2 time-staggered strangle"].itertuples():
        ev.append(dict(sleeve="L2", entry=r.entry, exit=r.exit, per_lot=r.pnl / 10, worst=0.0, spot=r.spot))
    g = gb.trades(1.0)
    for r in g[g.vix <= VIX_MAX].itertuples():
        ev.append(dict(sleeve="GAP", entry=r.entry, exit=r.exit, row=r))
    m = pd.read_csv("data/reports/monthly_straddle_vix_trades.csv")
    for r in m[m.vrp >= VRP_MIN].itertuples():
        ev.append(dict(sleeve="MON", entry=r.entry, exit=r.exit, per_lot=r.rs, worst=min(r.worst_pts, 0) * LOT,
                       spot=r.spot))
    return ev


def simulate(start, running=True, w_l2=W_L2, w_gap=W_GAP, w_mon=W_MON):
    ev = sorted(events(), key=lambda e: e["entry"])
    cash = float(start)
    open_, book = [], []          # book: (date, sleeve, pnl, note)
    months = pd.period_range(ev[0]["entry"][:7], max(e["exit"] for e in ev)[:7], freq="M")
    fixed = {str(p) + "-01": RUNNING for p in months} if running else {}

    def settle(upto):
        nonlocal cash
        for e in sorted([e for e in open_ if e["exit"] < upto], key=lambda e: e["exit"]):
            open_.remove(e)
            if e["sleeve"] == "GAP":
                pnl, _ = gap_pnl(e["row"], e["eq_at_entry"], w_gap)
            else:
                pnl = e["per_lot"] * e["lots"]
            cash += pnl
            book.append((e["exit"], e["sleeve"], pnl, e["lots"]))

    for e in ev:
        settle(e["entry"])
        for d in [d for d in list(fixed) if d <= e["entry"]]:
            cash -= fixed.pop(d)
            book.append((d, "running", -RUNNING, 0))
        e = dict(e, eq_at_entry=cash)
        if e["sleeve"] == "GAP":
            e["lots"] = gap_pnl(e["row"], cash, w_gap)[1]
        else:
            m_lot = MARGIN_PCT * e["spot"] * LOT
            lots = int((w_l2 if e["sleeve"] == "L2" else w_mon) * cash // m_lot)
            if e["sleeve"] == "L2" and lots < 3:
                lots = 0
            e["lots"] = lots
            if lots and e["worst"]:
                mid = pd.Timestamp(e["entry"]) + (pd.Timestamp(e["exit"]) - pd.Timestamp(e["entry"])) / 2
                book.append((mid.strftime("%Y-%m-%d"), "MON_mtm", e["worst"] * lots, lots))
                book.append((e["exit"], "MON_mtm", -e["worst"] * lots, lots))
        if e["lots"]:
            open_.append(e)
    settle("9999")
    B = pd.DataFrame(book, columns=["date", "sleeve", "pnl", "lots"]).sort_values("date", kind="stable")
    B["equity"] = start + B.pnl.cumsum()
    return B


def metrics(B, start, d0=None, d1=None):
    B = B[(B.date >= (d0 or "0")) & (B.date <= (d1 or "9"))]
    base = start if d0 is None else B.equity.iloc[0] - B.pnl.iloc[0]
    wk = B.groupby(pd.to_datetime(B.date).dt.to_period("W")).equity.last()
    e = np.concatenate([[base], wk.to_numpy()])
    yrs = (pd.Timestamp(B.date.iloc[-1]) - pd.Timestamp(d0 or "2021-01-04")).days / 365.25
    r = pd.Series(e).pct_change().dropna()
    real = B[B.sleeve.isin(["L2", "GAP", "MON"])]
    return dict(cagr=round(100 * ((e[-1] / base) ** (1 / yrs) - 1), 1),
                max_dd=round(100 * (e / np.maximum.accumulate(e) - 1).min(), 1),
                sharpe=round(r.mean() / r.std() * np.sqrt(52), 2), worst_week=round(100 * r.min(), 1),
                trades=real.groupby("sleeve").size().to_dict(),
                pnl_by_sleeve=real.groupby("sleeve").pnl.sum().round().astype(int).to_dict(),
                running_costs=int(-B[B.sleeve == "running"].pnl.sum()), final=int(e[-1]))


def by_year(B, start):
    out, prev = {}, start
    for y, g in B.groupby(B.date.str[:4]):
        out[y] = round(100 * (g.equity.iloc[-1] / prev - 1), 1)
        prev = g.equity.iloc[-1]
    return out


if __name__ == "__main__":
    summary = {}
    for start in [300_000, 1_000_000, 2_500_000]:
        for running in [True, False]:
            B = simulate(start, running)
            key = f"start={start} running_costs={running}"
            s = dict(full=metrics(B, start), IS=metrics(B, start, d1=IS_END), OOS=metrics(B, start, d0="2024-01-01"),
                     years=by_year(B, start), avg_lots=B[B.sleeve.isin(["L2", "MON"])].groupby("sleeve").lots.mean().round(1).to_dict())
            summary[key] = s
            if start == 1_000_000 and running:
                B.to_csv("data/reports/portfolio_strategy_trades.csv", index=False)
            f = s["full"]
            print(f"{key:38s} CAGR {f['cagr']:5.1f} DD {f['max_dd']:6.1f} Sh {f['sharpe']} worst wk {f['worst_week']} | "
                  f"IS {s['IS']['cagr']:5.1f}/{s['IS']['max_dd']:6.1f} OOS {s['OOS']['cagr']:5.1f}/{s['OOS']['max_dd']:6.1f} | "
                  f"lots {s['avg_lots']} | {f['pnl_by_sleeve']} | {s['years']}")
    with open("data/reports/portfolio_strategy_summary.json", "w") as fh:
        json.dump(summary, fh, indent=1, default=str)
