"""
Family 2: Positional weekly option structures (multi-day, contract-exact).

Entry on a chosen number of trading days before expiry (DTE), hold to expiry-day 15:15
unless the combined position hits its stop (loss >= stop_mult x net credit) or profit target
(>= tp_frac x net credit). All legs are tracked by absolute strike, minute by minute, across days.
MTM stop uses 1-min closes of all legs (a retail algo computes position MTM on LTP).
"""
import itertools
import numpy as np
import pandas as pd

from engine import load_days, load_daily, atm_strike
from cost_model import option_fill_cost, slip

QTY = 75
EXIT_MIN = 360     # 15:15 on expiry day
ENTRY_MIN = 15     # 09:30


def cycles(daily, days):
    """List of (entry_days_list_by_dte, expiry_date) for each weekly expiry with option data."""
    tds = [d for d in daily.index.strftime("%Y-%m-%d") if d in days]
    exp = set(daily.index[daily["is_expiry"] == True].strftime("%Y-%m-%d"))
    out = []
    prev = None
    for d in tds:
        if d in exp:
            window = [t for t in tds if (prev is None or t > prev) and t <= d]
            out.append((window, d))
            prev = d
    return out[1:]  # first cycle may be partial


def leg_path(days, window, k, typ, m0):
    """Concatenated close series of one contract from (window[0], m0) through expiry 15:15."""
    parts = []
    for i, d in enumerate(window):
        a = m0 if i == 0 else 0
        b = EXIT_MIN if i == len(window) - 1 else 375
        parts.append(days[d].series(k, typ, a, b))
    return np.concatenate(parts)


def day_index(window, m0):
    idx = []
    for i, d in enumerate(window):
        a = m0 if i == 0 else 0
        b = EXIT_MIN if i == len(window) - 1 else 375
        idx += [i] * (b - a + 1)
    return np.array(idx)


def run_cycle(days, window, legs_fn, stop_mult, tp_frac, m0=ENTRY_MIN):
    d0 = days[window[0]]
    spot = d0.spot[m0]
    legs = legs_fn(spot)  # list of (typ, strike, side) side=-1 short, +1 long
    paths = [leg_path(days, window, k, t, m0) for t, k, s in legs]
    entry = []
    for (t, k, s), p in zip(legs, paths):
        px = p[0]
        if s < 0 and px < 1.0:
            return None
        entry.append(px - slip(px) if s < 0 else px + slip(px))
    entry = np.array(entry)
    sides = np.array([s for _, _, s in legs])
    credit = -(sides * entry).sum()          # >0 for net credit structures
    P = np.vstack(paths)                      # legs x time
    mtm = (-(sides[:, None]) * (entry[:, None] - P)).sum(0) * -1  # per-unit P&L if closed at mid
    # clearer: pnl = sum(side * (P - entry))
    mtm = (sides[:, None] * (P - entry[:, None])).sum(0)
    ref = abs(credit) if credit != 0 else 1.0
    exit_i = len(mtm) - 1
    reason = "expiry"
    hit_sl = np.nonzero(mtm <= -stop_mult * ref)[0] if stop_mult else []
    hit_tp = np.nonzero(mtm >= tp_frac * ref)[0] if tp_frac else []
    cands = []
    if len(hit_sl):
        cands.append((hit_sl[0], "stop"))
    if len(hit_tp):
        cands.append((hit_tp[0], "target"))
    if cands:
        exit_i, reason = min(cands)
    urgent = reason == "stop"
    exitpx = []
    for (t, k, s), p in zip(legs, paths):
        px = p[exit_i]
        exitpx.append(px + slip(px, urgent) if s < 0 else max(0.05, px - slip(px, urgent)))
    exitpx = np.array(exitpx)
    pts = (sides * (exitpx - entry)).sum()
    date = window[0]
    cost = 0.0
    for (t, k, s), a, b in zip(legs, entry, exitpx):
        cost += option_fill_cost(a, QTY, s > 0, date) + option_fill_cost(b, QTY, s < 0, date)
    di = day_index(window, m0)[exit_i]
    return {"entry": window[0], "exit": window[di], "reason": reason, "credit": credit, "pts": pts,
            "rs": pts * QTY - cost, "cost": cost, "spot": spot, "dte": len(window) - 1}


def structure(kind, width, wing):
    def f(spot):
        K = atm_strike(spot)
        if kind == "strangle":
            return [("CE", K + width, -1), ("PE", K - width, -1)]
        if kind == "condor":
            return [("CE", K + width, -1), ("PE", K - width, -1),
                    ("CE", K + width + wing, +1), ("PE", K - width - wing, +1)]
    return f


def backtest(days, cyc, dte_entry, kind, width, wing, stop_mult, tp_frac):
    rows = []
    for window, exp in cyc:
        if len(window) < dte_entry + 1:
            continue
        w = window[-(dte_entry + 1):]
        r = run_cycle(days, w, structure(kind, width, wing), stop_mult, tp_frac)
        if r:
            rows.append(r)
    return pd.DataFrame(rows)


def stats(s):
    if len(s) == 0:
        return {}
    eq = s.cumsum()
    return {"n": len(s), "total": round(s.sum()), "avg": round(s.mean(), 1), "win%": round((s > 0).mean() * 100, 1),
            "maxDD": round((eq - eq.cummax()).min()), "sr_w": round(s.mean() / s.std() * np.sqrt(52), 2) if s.std() > 0 else 0,
            "worst": round(s.min())}


if __name__ == "__main__":
    days = load_days()
    daily = load_daily()
    cyc = cycles(daily, days)
    print("cycles:", len(cyc))
    rows = []
    grid = itertools.product([1, 2, 4], ["strangle", "condor"], [150, 250, 400], [200],
                             [1.0, 2.0, None], [0.5, None])
    for dte_e, kind, width, wing, sm, tp in grid:
        df = backtest(days, cyc, dte_e, kind, width, wing, sm, tp)
        if df.empty:
            continue
        df["yr"] = df["entry"].str[:4]
        IS = df[df.entry < "2024-01-01"]["rs"]
        OOS = df[df.entry >= "2024-01-01"]["rs"]
        by_year = df.groupby("yr")["rs"].sum().round().to_dict()
        rows.append({"dte": dte_e, "kind": kind, "width": width, "stop": sm, "tp": tp,
                     **{f"IS_{k}": v for k, v in stats(IS).items()},
                     **{f"OOS_{k}": v for k, v in stats(OOS).items()}, **by_year})
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/research_positional.csv", index=False)
    pd.set_option("display.width", 250)
    print(res.sort_values("IS_sr_w", ascending=False).to_string())
