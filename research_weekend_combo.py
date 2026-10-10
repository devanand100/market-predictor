"""
Weekend theta strangle (Trading Bot/weekend_theta_lambda/backtest_reference.py) re-tested on this
repo's contract-exact engine, then combined with the bear-call spread (research_etf_overlay.py).

Weekend rules (unchanged from the reference):
  - Entry Friday 15:15 if India VIX >= 12 (VIX at 15:15, not the daily close: no look-ahead).
  - Sell CE and PE at Black-Scholes delta = clip(0.10 + 0.006*VIX, 0.15, 0.22), sigma = VIX, T = 6/365.
  - Monday 09:20: if either short leg >= 2.5x its sell price, close both at 09:20. Else close Monday 15:15.
Differences from the reference:
  - Legs that drift outside Dhan's +/-500-point window are priced at intrinsic (engine.Day.px) instead of
    being DROPPED. The reference silently skips those trades, which are exactly the big-gap weeks.
  - Fridays that are themselves expiry days are skipped (Friday's contract dies that day; Monday's is another).
  - Date-aware statutory costs and the repo slippage model (0.3 + 0.5% of premium; 0.5 + 1.5% on the stop).

Combination (daily equity, P&L booked on exit day):
  - Bear calls sized as before (8% of equity at max loss, min 1 lot), margin SPREAD_MARGIN per lot.
  - Strangle margin = 9% of notional per lot (same rule as Strategy 1). Lots = floor((MAX_USE * equity -
    margin already open) / margin per lot); 0 if it doesn't fit.
Writes data/reports/weekend_theta_trades.csv and weekend_combo_summary.csv.
"""
import math

import numpy as np
import pandas as pd

import final_spread_strategy as fss
import research_etf_overlay as ov
from cost_model import option_fill_cost, slip
from engine import load_days, load_daily

LOT = 65
M_FRI, M_0920, M_1515 = 360, 5, 360
SL_MULT = 2.5
VIX_MIN = 12.0
STRANGLE_MARGIN_PCT = 0.09
SPREAD_MARGIN = ov.SPREAD_MARGIN


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def strike_for_delta(spot, vix, target, typ, T=6 / 365, r=0.07):
    sig = max(0.08, vix / 100)
    atm = int(round(spot / 50) * 50)
    rng = range(atm + 50, atm + 1500, 50) if typ == "CE" else range(atm - 50, atm - 1500, -50)
    best, bd = atm, 9.0
    for k in rng:
        d1 = (math.log(spot / k) + (r + 0.5 * sig ** 2) * T) / (sig * math.sqrt(T))
        delta = ncdf(d1) if typ == "CE" else 1 - ncdf(d1)
        if abs(delta - target) < bd:
            best, bd = k, abs(delta - target)
    return best


def vix_1515():
    v = pd.read_parquet("data/raw/minute_parquet/india_vix_1min.parquet", columns=["date", "time", "close"])
    return v[v.time == "15:15"].set_index("date")["close"]


def weekend_trades(days, daily):
    vix = vix_1515()
    exp = set(daily.index[daily.is_expiry == True].strftime("%Y-%m-%d"))
    rows, dropped_by_reference = [], 0
    for fri in sorted(days):
        f = pd.Timestamp(fri)
        mon = (f + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
        if f.dayofweek != 4 or mon not in days or fri in exp:
            continue
        v = vix.get(fri)
        if v is None or v < VIX_MIN:
            continue
        D, M = days[fri], days[mon]
        spot = D.spot[M_FRI]
        delta = min(0.22, max(0.15, 0.10 + 0.006 * v))
        legs = {"CE": strike_for_delta(spot, v, delta, "CE"), "PE": strike_for_delta(spot, v, delta, "PE")}
        # Would the reference have dropped this trade (any needed price missing from the window)?
        outside = any(M.col(k) is None or D.col(k) is None for k in legs.values())
        dropped_by_reference += outside
        sell = {t: D.px(k, t, M_FRI) for t, k in legs.items()}
        sell_fill = {t: p - slip(p) for t, p in sell.items()}
        at0920 = {t: M.px(k, t, M_0920) for t, k in legs.items()}
        stop = any(at0920[t] >= SL_MULT * sell_fill[t] for t in legs)
        m_exit = M_0920 if stop else M_1515
        out = {t: M.px(k, t, m_exit) for t, k in legs.items()}
        buy_fill = {t: p + slip(p, urgent=stop) for t, p in out.items()}
        pts = sum(sell_fill[t] - buy_fill[t] for t in legs)
        cost = sum(option_fill_cost(sell_fill[t], LOT, False, fri) + option_fill_cost(buy_fill[t], LOT, True, mon) for t in legs)
        rows.append({"entry": fri, "exit": mon, "vix": round(v, 2), "spot": round(spot, 1), "delta": round(delta, 3),
                     "ce": legs["CE"], "pe": legs["PE"], "credit": round(sum(sell_fill.values()), 2),
                     "stop": stop, "pts": round(pts, 2), "rs": pts * LOT - cost, "cost": round(cost, 1),
                     "mon_move_pct": round((M.spot[m_exit] / spot - 1) * 100, 2), "outside_window": outside})
    return pd.DataFrame(rows), dropped_by_reference


def simulate(events, nifty, start, max_use=0.6, cash_yield=0.0):
    """events: DataFrame entry, exit, rs (per lot), kind ('bear'|'wknd'), spot."""
    by_day = {}
    for r in events.itertuples():
        by_day.setdefault(pd.Timestamp(r.entry), []).append(r)
    t0 = pd.Timestamp(events.entry.min())
    cash, booked, margin_open = float(start), {}, {}
    curve, peak_use, lots_log = [], 0.0, []
    for d in nifty.index[nifty.index >= t0]:
        if curve:
            cash *= (1 + cash_yield) ** (1 / 252)
        cash += booked.pop(d, 0.0)
        for k in [k for k in margin_open if k[0] <= d]:
            margin_open.pop(k)
        for r in sorted(by_day.get(d, []), key=lambda r: r.kind != "bear"):   # bear call is entered first
            used = sum(margin_open.values())
            if r.kind == "bear":
                lots = max(1, int(ov.RISK * cash // (200 * LOT)))
                m = lots * SPREAD_MARGIN
            else:
                per = STRANGLE_MARGIN_PCT * r.spot * LOT
                lots = int(max(0.0, max_use * cash - used) // per)
                m = lots * per
            if lots == 0:
                continue
            booked[pd.Timestamp(r.exit)] = booked.get(pd.Timestamp(r.exit), 0.0) + r.rs * lots
            margin_open[(pd.Timestamp(r.exit), r.kind, r.entry)] = m
            peak_use = max(peak_use, (used + m) / cash)
            lots_log.append((r.kind, lots))
        curve.append((d, cash))
    eq = pd.Series(dict(curve))
    lots = pd.DataFrame(lots_log, columns=["kind", "lots"])
    return eq, peak_use, lots


def main():
    days, daily = load_days(), load_daily()
    nifty = daily["close"].dropna()
    wk, dropped = weekend_trades(days, daily)
    wk.to_csv("data/reports/weekend_theta_trades.csv", index=False)

    # ---- Standalone weekend strangle, per lot
    r = wk.rs
    print(f"WEEKEND STRANGLE (contract-exact), {len(wk)} trades {wk.entry.iloc[0]} .. {wk.entry.iloc[-1]}")
    print(f"  reference would have DROPPED {dropped} of these (leg outside the data window)")
    print(f"  per lot: total ₹{r.sum():,.0f}, avg ₹{r.mean():,.0f}, win {(r > 0).mean():.0%}, "
          f"worst ₹{r.min():,.0f}, stops {wk.stop.sum()}, PF {r[r > 0].sum() / -r[r <= 0].sum():.2f}")
    wk["yr"] = wk.entry.str[:4]
    print(wk.groupby("yr").rs.agg(n="size", total="sum", avg="mean", worst="min",
                                  win=lambda s: (s > 0).mean() * 100).round(0).to_string())
    print("  worst 6:", wk.nsmallest(6, "rs")[["entry", "vix", "mon_move_pct", "stop", "rs"]].round(1).to_dict("records"))
    print(f"  IS 2021-23 avg ₹{r[wk.entry < '2024'].mean():,.0f}/trade, OOS 2024-26 avg ₹{r[wk.entry >= '2024'].mean():,.0f}/trade")

    # ---- Bear calls
    df, _ = fss.spread_trades()
    bear = ov.trade_variants(df)["bear calls"]
    ev = pd.concat([bear[["entry", "exit", "rs", "spot"]].assign(kind="bear"),
                    wk[["entry", "exit", "rs", "spot"]].assign(kind="wknd")])
    ev = ev[ev.entry >= bear.entry.min()].sort_values("entry")

    # Overlap & correlation (per lot, by week of exit)
    wkp = wk.assign(w=pd.to_datetime(wk.exit).dt.to_period("W")).groupby("w").rs.sum()
    bcp = bear.assign(w=pd.to_datetime(bear.exit).dt.to_period("W")).groupby("w").rs.sum()
    j = pd.concat([wkp.rename("wknd"), bcp.rename("bear")], axis=1).fillna(0)
    j = j[j.index >= pd.Period(bear.entry.min(), "W")]
    over = 0
    for b in bear.itertuples():
        over += ((wk.entry >= b.entry) & (wk.exit <= b.exit)).sum()
    print(f"\nOVERLAP: {over} weekend trades fall inside an open bear-call position (of {len(bear)} bear calls)")
    print(f"  weekly P&L correlation (per lot): {j.wknd.corr(j.bear):.2f}")
    both = j[(j.wknd != 0) & (j.bear != 0)]
    print(f"  weeks with both: {len(both)}; both lost in same week: {((both.wknd < 0) & (both.bear < 0)).sum()}")

    # ---- Sized portfolios
    rows = []
    for cap in (200_000, 300_000, 500_000, 1_000_000):
        for mu in (0.6, 0.9):
            for name, e in (("bear calls only", ev[ev.kind == "bear"]), ("weekend only", ev[ev.kind == "wknd"]),
                            ("bear + weekend", ev)):
                if name == "bear calls only" and mu != 0.6:
                    continue
                for cy in (0.0, 0.06):
                    eq, pu, lots = simulate(e, nifty, cap, mu, cy)
                    s = ov.stats(eq)
                    nw = lots[lots.kind == "wknd"]
                    rows.append({"capital": cap, "portfolio": name, "max_margin_use": mu, "cash_yield": cy,
                                 "cagr_pct": s["cagr_pct"], "max_dd_pct": s["max_dd_pct"], "sharpe": s["sharpe"],
                                 "worst_week_pct": s["worst_week_pct"], "final": s["final"], "peak_margin_pct": pu * 100,
                                 "wknd_trades": len(nw), "wknd_avg_lots": nw.lots.mean() if len(nw) else 0,
                                 **{y: s[y] for y in s if y.isdigit()}})
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/weekend_combo_summary.csv", index=False)
    pd.set_option("display.width", 250, "display.max_columns", 40)
    print(res.round(1).to_string(index=False))


if __name__ == "__main__":
    main()
