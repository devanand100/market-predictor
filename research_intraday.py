"""
Family 1: Intraday premium selling (short straddle / strangle / iron-fly) on the nearest
weekly Nifty expiry, contract-exact, 1-minute stop-loss simulation.

Fill model
  entry  : sell at 1-min close - slip ; hedge buy at close + slip
  SL     : triggers when 1-min HIGH >= SL; fill = max(SL, bar close) + urgent slip
  exit   : at exit minute close + slip (always before 15:30, no exercise STT)
P&L is computed per unit in points, then Rs for a given quantity including statutory costs.
"""
import itertools
import numpy as np
import pandas as pd

from engine import load_days, load_daily, atm_strike
from cost_model import option_fill_cost, slip

QTY = 75  # units per "lot-equivalent" used for cost scaling in research


def dte_map(daily, dates):
    exp = daily.index[daily["is_expiry"] == True]
    out = {}
    tds = daily.index
    for d in dates:
        ts = pd.Timestamp(d)
        nxt = exp[exp >= ts]
        if len(nxt) == 0:
            out[d] = np.nan
            continue
        out[d] = int(((tds >= ts) & (tds < nxt[0])).sum())
    return out


def sim_short_leg(day, strike, typ, e, x, sl_pct, trail_to_cost_flag=None):
    """Returns (entry_fill, exit_fill, exit_minute, stopped)."""
    p0 = day.px(strike, typ, e)
    entry = p0 - slip(p0)
    if entry <= 0.5:
        return None
    sl = entry * (1 + sl_pct) if sl_pct is not None else np.inf
    hi = day.series(strike, typ, e + 1, x, high=True)
    cl = day.series(strike, typ, e + 1, x)
    hit = np.nonzero(hi >= sl)[0]
    if len(hit):
        i = hit[0]
        fill = max(sl, cl[i])
        return entry, fill + slip(fill, urgent=True), e + 1 + i, True
    px = cl[-1]
    return entry, px + slip(px), x, False


def run_day(day, date, e, x, width_pts, sl_pct, wing_pts=None, move_to_cost=False):
    """Short CE at K+width, PE at K-width (width 0 = straddle). Optional long wings."""
    K = atm_strike(day.spot[e])
    legs = [("CE", K + width_pts), ("PE", K - width_pts)]
    res = []
    for typ, k in legs:
        r = sim_short_leg(day, k, typ, e, x, sl_pct)
        if r is None:
            return None
        res.append((typ, k) + r)
    if move_to_cost:
        # after the first leg stops, the surviving leg's SL moves to its entry price
        stopped = [r for r in res if r[5]]
        if len(stopped) == 1:
            m_stop = stopped[0][4]
            for idx, r in enumerate(res):
                if not r[5]:
                    typ, k, entry = r[0], r[1], r[2]
                    hi = day.series(k, typ, m_stop + 1, x, high=True)
                    cl = day.series(k, typ, m_stop + 1, x)
                    hit = np.nonzero(hi >= entry)[0] if m_stop < x else []
                    if len(hit):
                        i = hit[0]
                        fill = max(entry, cl[i])
                        res[idx] = (typ, k, entry, fill + slip(fill, urgent=True), m_stop + 1 + i, True)
    pts = sum(r[2] - r[3] for r in res)
    cost = sum(option_fill_cost(r[2], QTY, False, date) + option_fill_cost(r[3], QTY, True, date) for r in res)
    credit = sum(r[2] for r in res)
    if wing_pts:
        for typ, k in [("CE", K + width_pts + wing_pts), ("PE", K - width_pts - wing_pts)]:
            p_in = day.px(k, typ, e)
            b = p_in + slip(p_in)
            # wing closed when its short leg closes
            leg = [r for r in res if r[0] == typ][0]
            p_out = day.px(k, typ, leg[4])
            s = max(0.05, p_out - slip(p_out))
            pts += s - b
            credit -= b
            cost += option_fill_cost(b, QTY, True, date) + option_fill_cost(s, QTY, False, date)
    nstop = sum(r[5] for r in res)
    return {"date": date, "K": K, "credit": credit, "pts": pts, "rs": pts * QTY - cost,
            "cost": cost, "nstop": nstop, "spot": day.spot[e]}


def backtest(days, dates, **kw):
    rows = []
    for d in dates:
        r = run_day(days[d], d, **kw)
        if r:
            rows.append(r)
    return pd.DataFrame(rows)


def stats(df, col="rs"):
    if df is None or len(df) == 0:
        return {}
    s = df[col]
    eq = s.cumsum()
    return {"n": len(s), "total": round(s.sum()), "avg": round(s.mean(), 1), "win%": round((s > 0).mean() * 100, 1),
            "maxDD": round((eq - eq.cummax()).min()), "sharpe_d": round(s.mean() / s.std() * np.sqrt(252), 2) if s.std() > 0 else 0,
            "worst": round(s.min())}


if __name__ == "__main__":
    days = load_days()
    daily = load_daily()
    dates = sorted(days.keys())
    dte = dte_map(daily, dates)
    IS = [d for d in dates if d < "2024-01-01"]
    OOS = [d for d in dates if d >= "2024-01-01"]

    rows = []
    grid = itertools.product([5, 30, 75], [0, 100], [0.25, 0.4, 0.6, None], [None, 300], [False, True])
    for e, width, sl, wing, mtc in grid:
        if sl is None and mtc:
            continue
        df = backtest(days, IS, e=e, x=360, width_pts=width, sl_pct=sl, wing_pts=wing, move_to_cost=mtc)
        df["dte"] = df["date"].map(dte)
        for bucket, sub in [("all", df), ("dte0", df[df.dte == 0]), ("dte1", df[df.dte == 1]),
                            ("dte2+", df[df.dte >= 2])]:
            st = stats(sub)
            rows.append({"entry_min": e, "width": width, "sl": sl, "wing": wing, "mtc": mtc, "bucket": bucket, **st})
        print(e, width, sl, wing, mtc, stats(df))
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/research_intraday_IS.csv", index=False)
    print(res.sort_values("sharpe_d", ascending=False).head(40).to_string())
