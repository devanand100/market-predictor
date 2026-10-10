"""
Bear-call spread + Nifty ETF overlay (small-account research).

Strategy 2's credit spreads use little of the account: the rest sits idle as cash. Here a
fraction `w` of the account is held in a Nifty ETF and the spreads keep their normal size
(8% of TOTAL equity at max loss), so the ETF is extra exposure funded by idle cash.
Idea: bear calls lose in sharp rallies, exactly when the ETF gains.

Model (daily mark-to-market, from the first spread entry to the last data day):
  - Spread trades: final_spread_strategy.spread_trades() on the current data (per-lot P&L net of
    fees and slippage). Variants: bear calls only / v2 (bull puts half size), both with the
    reward-to-risk filter credit / (200 - credit) >= 0.232.
  - ETF = Nifty 50 price index close (dividends ~1.2%/yr ignored, expense ratio ignored: conservative),
    rebalanced to weight w on the first trading day of each month, 0.05% cost on the traded amount.
  - Cash earns CASH_YIELD (0 by default; LIQUIDBEES-style ~6% shown separately).
  - Spread P&L is booked on the exit day. No fixed running costs by default (manual execution);
    the ₹1,990/month case is shown separately.
  - Margin check: SPREAD_MARGIN per lot; the ETF can be pledged (10% haircut) but at least half the
    margin must be cash. Reports the peak margin usage and whether that rule was ever broken.
Pre-tax: ETF gains are capital gains, option P&L is business income.

Writes data/reports/etf_overlay_summary.csv and etf_overlay_equity.csv.
"""
import numpy as np
import pandas as pd

import final_spread_strategy as fss
from research_directional_spreads import SPREAD_W

LOT = 65
RISK = 0.08
RR_MIN = 0.232
SPREAD_MARGIN = 40_000      # ₹ per lot for a 200-point Nifty credit spread (approx., conservative)
REBAL_COST = 0.0005
RUNNING = fss.RUNNING


def trade_variants(df):
    df = df[df.credit / (SPREAD_W - df.credit) >= RR_MIN].copy()
    bear = df[df.dir == "bear call"].assign(size=1.0)
    v2 = df.assign(size=np.where(df.dir == "bull put", 0.5, 1.0))
    return {"bear calls": bear, "v2 (bull puts half)": v2}


def simulate(trades, nifty, start, w, cash_yield=0.0, running=False):
    trades = trades.sort_values("entry")
    days = nifty.index[nifty.index >= pd.Timestamp(trades.entry.iloc[0])]
    by_entry = {pd.Timestamp(e): r for e, r in zip(trades.entry, trades.itertuples())}
    open_pos, units, cash = {}, 0.0, float(start)
    rows, last_m, peak_margin, rule_broken = [], None, 0.0, False
    for i, d in enumerate(days):
        px = nifty[d]
        if i:
            cash *= (1 + cash_yield) ** (1 / 252)
        m = d.strftime("%Y-%m")
        if m != last_m:
            if running:
                cash -= RUNNING
            eq = cash + units * px
            target = w * eq / px
            cash -= (target - units) * px + abs(target - units) * px * REBAL_COST
            units, last_m = target, m
        eq = cash + units * px
        if d in by_entry:
            r = by_entry[d]
            lots = max(1, int(RISK * r.size * eq // (SPREAD_W * LOT)))
            open_pos[pd.Timestamp(r.exit)] = open_pos.get(pd.Timestamp(r.exit), 0) + r.rs * lots
            margin = lots * SPREAD_MARGIN
            peak_margin = max(peak_margin, margin / eq)
            if cash < 0.5 * margin or cash + 0.9 * units * px < margin:
                rule_broken = True
        cash += open_pos.pop(d, 0.0)
        rows.append((d, cash + units * px))
    eq = pd.Series(dict(rows))
    return eq, peak_margin, rule_broken


def stats(eq, nifty_ret=None):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    wk = eq.resample("W-FRI").last().pct_change().dropna()
    out = {"cagr_pct": ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100,
           "max_dd_pct": (eq / eq.cummax() - 1).min() * 100,
           "sharpe": wk.mean() / wk.std() * np.sqrt(52),
           "worst_week_pct": wk.min() * 100,
           "final": eq.iloc[-1]}
    for y, g in eq.groupby(eq.index.year):
        prev = eq[eq.index.year < y]
        base = prev.iloc[-1] if len(prev) else g.iloc[0]
        out[str(y)] = (g.iloc[-1] / base - 1) * 100
    return out


def main():
    df, daily = fss.spread_trades()
    nifty = daily["close"].dropna()
    variants = trade_variants(df)

    # Diagnostics: does the ETF hedge the bear calls? Weekly Nifty move vs bear-call P&L per lot
    bear = variants["bear calls"]
    mv = [nifty[pd.Timestamp(x)] / nifty[nifty.index < pd.Timestamp(e)].iloc[-1] - 1 for e, x in zip(bear.entry, bear.exit)]
    bear = bear.assign(nifty_move=np.array(mv) * 100)
    print(f"Bear-call trades after RR filter: {len(bear)}, win {(bear.rs > 0).mean():.0%}, "
          f"corr(P&L, Nifty move over trade) = {np.corrcoef(bear.rs, bear.nifty_move)[0, 1]:.2f}")
    print("Bear-call losses by Nifty move:", bear[bear.rs < 0][["entry", "nifty_move", "rs"]].round(1).to_dict("records"))

    rows, curves = [], {}
    configs = [(v, w) for v in variants for w in (0.0, 0.25, 0.5, 0.75)]
    for cap in (200_000, 500_000):
        for v, w in configs:
            for label, kw in (("base", {}), ("cash 6%", {"cash_yield": 0.06}), ("fixed costs", {"running": True})):
                if label != "base" and (cap != 200_000 or w not in (0.0, 0.5)):
                    continue
                eq, pm, broken = simulate(variants[v], nifty, cap, w, **kw)
                for period, e in (("all", eq), ("2022-23", eq[:"2023-12-31"]), ("2024-26", eq["2024-01-01":])):
                    if label != "base" and period != "all":
                        continue
                    rows.append({"capital": cap, "variant": v, "etf_w": w, "case": label, "period": period,
                                 **stats(e), "peak_margin_pct": pm * 100, "margin_rule_broken": broken})
                if label == "base" and cap == 200_000:
                    curves[f"{v} | ETF {int(w * 100)}%"] = eq
        # ETF alone for reference
        e = nifty[nifty.index >= pd.Timestamp(df.entry.iloc[0])] / nifty[nifty.index >= pd.Timestamp(df.entry.iloc[0])].iloc[0] * cap
        for period, ee in (("all", e), ("2022-23", e[:"2023-12-31"]), ("2024-26", e["2024-01-01":])):
            rows.append({"capital": cap, "variant": "Nifty only", "etf_w": 1.0, "case": "base", "period": period, **stats(ee)})

    res = pd.DataFrame(rows)
    res.to_csv("data/reports/etf_overlay_summary.csv", index=False)
    pd.DataFrame(curves).to_csv("data/reports/etf_overlay_equity.csv")
    pd.set_option("display.width", 250, "display.max_columns", 30)
    cols = ["capital", "variant", "etf_w", "case", "period", "cagr_pct", "max_dd_pct", "sharpe", "worst_week_pct",
            "peak_margin_pct", "margin_rule_broken"]
    print(res[cols].round(1).to_string(index=False))
    yr = [c for c in res.columns if c.isdigit()]
    print(res[(res.period == "all") & (res.case == "base") & (res.capital == 200_000)][["variant", "etf_w"] + yr].round(1).to_string(index=False))


if __name__ == "__main__":
    main()
