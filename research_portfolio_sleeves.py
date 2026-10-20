"""
Multi-sleeve portfolio search: can 25-35% CAGR come with a controlled drawdown?

Weekly return streams per unit of capital deployed (fractional sizing, costs included):
  S1   weekly strangle, Strategy 1 (per 100% margin; final_strategy trades)
  D2   delta-hedged strangle, band 1 lot (research_institutional_trades.csv, ret per 60% use)
  L2   time-staggered strangle (same file)
  GAP  gap buy on VIX <= 15 weeks (per 100% of equity in premium; final_gap_buy trades)
  MON  monthly 45->21 DTE short straddle, VRP >= 2 (monthly_straddle_vix_trades.csv, per margin)
  BC   Strategy 2 bear calls with the v2 credit filter (per 100% of equity at max loss)
Each trade's return is booked in the week of its exit. Monthly straddles therefore show as one
lump; their intra-trade drawdown is not seen (see caveats in the printout).

Selection: weights on a coarse grid, judged on 2021-23 only: highest IS CAGR with IS max DD
better than DD_CAP and total margin <= MARGIN_CAP. 2024-26 and a Monte Carlo are reported.
Writes data/reports/portfolio_sleeves_grid.csv
"""
import itertools
import numpy as np
import pandas as pd

import final_strategy as fs
import final_gap_buy as gb
import final_spread_strategy as fss
from research_directional_spreads import SPREAD_W
from cost_model import option_fill_cost

LOT = 65
IS_END = "2023-12-31"
DD_CAP = -20.0
MARGIN_CAP = 0.60      # project rule: keep 40% of equity free for MTM swings and margin hikes


def wkey(d):
    t = pd.Timestamp(d)
    return (t - pd.Timedelta(days=t.weekday())).strftime("%Y-%m-%d")   # Monday of the exit week


def d2_pessimistic(path="data/reports/d2_pessimistic_trades.csv"):
    """D2 (delta-hedged strangle, 1-lot band, 10 lots) under futures slippage 2 pts, basis drift
    5 pts/day against the hedge and ATM IV (the 'all three' stress case). Cached."""
    try:
        return pd.read_csv(path)
    except FileNotFoundError:
        pass
    import cost_model
    import research_institutional as ri
    import research_positional as rp
    from engine import load_days, load_daily
    cost_model.FUT_SLIP_PTS, ri.BASIS_PTS_PER_DAY, ri.ATM_IV_ONLY = 2.0, 5.0, True
    d, dl = load_days(), load_daily()
    t = pd.DataFrame([r for w, e in rp.cycles(dl, d) if (r := ri.run(d, w, "D2", band_lots=1))])
    cost_model.FUT_SLIP_PTS, ri.BASIS_PTS_PER_DAY, ri.ATM_IV_ONLY = 1.0, 0.0, False
    t.to_csv(path, index=False)
    return t


def streams():
    out = {}
    s1, daily = fs.trades()
    out["S1"] = pd.Series({wkey(r.exit): r.rs / (fs.MARGIN_PCT * r.spot * LOT) for r in s1.itertuples()})
    inst = pd.read_csv("data/reports/research_institutional_trades.csv")
    t = inst[inst.strategy == "L2 time-staggered strangle"]
    out["L2"] = t.groupby(t.exit.map(wkey)).ret_pct.sum() / 100 / fs.MAX_MARGIN_USE
    t = d2_pessimistic()
    out["D2"] = t.groupby(t.exit.map(wkey)).ret_pct.sum() / 100 / fs.MAX_MARGIN_USE
    g = gb.trades(1.0)
    g = g[g.vix <= 15]
    q = 2 * LOT
    out["GAP"] = pd.Series({wkey(r.exit): ((r.sell - r.buy) * q - option_fill_cost(r.buy, q, True, r.entry)
                                            - option_fill_cost(r.sell, q, False, r.exit)) / (r.buy * q)
                            for r in g.itertuples()})
    m = pd.read_csv("data/reports/monthly_straddle_vix_trades.csv")
    m = m[m.vrp >= 2]
    # Book the worst open loss mid-trade and the recovery at exit, so drawdowns see it
    mon = []
    for r in m.itertuples():
        unit = LOT / (fs.MARGIN_PCT * r.spot * LOT)
        worst = min(r.worst_pts, 0) * unit
        mid = pd.Timestamp(r.entry) + (pd.Timestamp(r.exit) - pd.Timestamp(r.entry)) / 2
        mon += [(wkey(mid), worst), (wkey(r.exit), r.rs / (fs.MARGIN_PCT * r.spot * LOT) - worst)]
    out["MON"] = pd.DataFrame(mon).groupby(0)[1].sum()
    fss.rp.QTY = LOT
    sp, _ = fss.spread_trades()
    sp = sp[(sp.dir == "bear call") & (sp.credit / (SPREAD_W - sp.credit) >= 0.232)]
    out["BC"] = pd.Series({wkey(r.exit): r.rs / (SPREAD_W * LOT) for r in sp.itertuples()})
    idx = pd.date_range("2021-01-04", max(s.index.max() for s in out.values()), freq="W-MON").strftime("%Y-%m-%d")
    return pd.DataFrame({k: v.groupby(level=0).sum().reindex(idx, fill_value=0.0) for k, v in out.items()})


def perf(r):
    e = np.concatenate([[1.0], np.cumprod(1 + r)])
    yrs = len(r) / 52.18
    return 100 * (e[-1] ** (1 / yrs) - 1), 100 * (e / np.maximum.accumulate(e) - 1).min()


def mc(r, n=4000, years=5, seed=7):
    P = np.cumprod(1 + np.random.default_rng(seed).choice(r, (n, 52 * years)), 1)
    dd = (P / np.maximum.accumulate(P, 1) - 1).min(1)
    return (round(100 * (np.median(P[:, -1]) ** (1 / years) - 1), 1), round(100 * np.median(dd), 1),
            round(100 * np.percentile(dd, 5), 1))


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    W = streams()
    W.to_csv("data/reports/portfolio_sleeves_weekly.csv")
    isr = W.index <= IS_END
    print("Per-sleeve (unit size) CAGR / maxDD, IS | OOS:")
    for c in W:
        a, b = perf(W[c][isr].to_numpy()), perf(W[c][~isr].to_numpy())
        print(f"  {c:4s} IS {a[0]:7.1f}/{a[1]:6.1f}  OOS {b[0]:7.1f}/{b[1]:6.1f}")
    print("\nWeekly correlation (weeks where either traded):")
    print(W.replace(0, np.nan).corr(min_periods=20).round(2))

    grid = {"vol": [("S1", u) for u in [0, .2, .3, .4, .5, .6]] + [("D2", u) for u in [.3, .45, .6]]
                   + [("L2", u) for u in [.3, .45, .6]],
            "GAP": [0, .02, .03, .04, .05],
            "MON": [0, .1, .2, .3],
            "BC": [0, .04, .08]}
    rows = []
    for (vn, vu), g, mo, bc in itertools.product(grid["vol"], grid["GAP"], grid["MON"], grid["BC"]):
        # spread margin ~ max loss; D2 adds futures-hedge margin (~25% on top of the strangle's)
        margin = vu * (1.25 if vn == "D2" else 1.0) + mo + bc * 1.0
        if margin > MARGIN_CAP:
            continue
        r = vu * W[vn] + g * W["GAP"] + mo * W["MON"] + bc * W["BC"]
        ic, idd = perf(r[isr].to_numpy())
        oc, odd = perf(r[~isr].to_numpy())
        ac, add = perf(r.to_numpy())
        rows.append(dict(vol=vn, vol_m=vu, gap=g, mon=mo, bc=bc, margin=round(margin, 2), is_cagr=round(ic, 1),
                         is_dd=round(idd, 1), oos_cagr=round(oc, 1), oos_dd=round(odd, 1), all_cagr=round(ac, 1),
                         all_dd=round(add, 1), worst_wk=round(100 * r.min(), 1)))
    G = pd.DataFrame(rows)
    pick = G[G.is_dd >= DD_CAP].sort_values("is_cagr", ascending=False).head(12).copy()
    pick[["mc_cagr", "mc_dd_med", "mc_dd_1in20"]] = [
        mc((r.vol_m * W[r.vol] + r.gap * W["GAP"] + r.mon * W["MON"] + r.bc * W["BC"]).to_numpy())
        for r in pick.itertuples()]
    G.to_csv("data/reports/portfolio_sleeves_grid.csv", index=False)
    print(f"\nTop 12 by IS CAGR with IS DD >= {DD_CAP}% and margin <= {MARGIN_CAP:.0%} (OOS untouched):")
    print(pick.to_string(index=False))
    for cap in [-15, -25]:
        p = G[G.is_dd >= cap].sort_values("is_cagr", ascending=False).head(3)
        print(f"\nIS DD cap {cap}%:")
        print(p.to_string(index=False))
