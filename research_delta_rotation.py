"""
"Delta-Rotation Credit Spread Expiry" (a pasted marketing description) on Dhan 1-minute data.
docs/DELTA_ROTATION_SPREAD.md has the reading, the results and the verdict.

The description gives no formulas, so this is one fixed reading, set before any P&L was seen:
  checkpoints  every 30 min, 10:00 .. 14:30 (minute 45 .. 315); signal from bar m-1, fill at bar m;
               no new entry on expiry day; one position at a time
  smile        Dhan IV of the out-of-the-money side: put at ATM-200, call at ATM+200, ATM = mean of
               the two ATM IVs. skew = put200 - call200, curv = (put200 + call200)/2 - atm
  alpha        z(spot return since 09:30) - z(skew change since 09:30)   (up + falling put skew = bull)
  momentum     alpha - alpha at the previous checkpoint (same sign as alpha required)
  alpha2       z(30-min spot return), only if curvature did not rise over those 30 min
  energy gate  ("Hamiltonian") z(r30)^2 + z(d_atm_iv30)^2 >= 2; eigenvalues / entropy: undefined, left out
  bull         alpha > th, momentum > 0, alpha2 > 0 -> sell ATM put, buy put W lower
  bear         mirror -> sell ATM call, buy call W higher   (the text's "ITM" put hedge would be a
               debit spread; the OTM hedge is used)
  exits        target: profit >= 50% of the credit; stop: loss >= stop x margin (margin = W x qty);
               time: 15:15 of the entry session (hold 0) or the next one (hold 1), never past
               expiry-day 15:15. Minute-close marks, slippage and charges from cost_model.
z-scores divide by the std of the same feature over the previous 200 checkpoints (~20 sessions).
Design 2021-2023, out of sample 2024 .. 2026-10. Account: lots = 25% of equity / (W x lot), i.e. a quarter of the equity is margin
(100% went below zero). Sharpe uses no risk-free rate.
Usage: python research_delta_rotation.py
"""
import itertools
import math

import numpy as np
import pandas as pd

from cost_model import get_lot_size, option_fill_cost, slip
from engine import NMIN, atm_strike, load_daily, load_days

CHECK = list(range(45, 316, 30))
EXIT_MIN = 360
CAPITAL = 500_000
OUT = "data/reports/delta_rotation"
SPLIT = "2024-01-01"


# --- features ------------------------------------------------------------------------------

def iv_at(d, k, typ, m):
    j = d.col(k)
    if j is None:
        return np.nan
    v = float((d.ce_iv if typ == "CE" else d.pe_iv)[m, j])
    return v if 1.0 < v < 200.0 else np.nan


def smile(d, m):
    K = atm_strike(d.spot[m])
    atm = np.nanmean([iv_at(d, K, "CE", m), iv_at(d, K, "PE", m)])
    p, c = iv_at(d, K - 200, "PE", m), iv_at(d, K + 200, "CE", m)
    return atm, p - c, (p + c) / 2 - atm


def features(days, daily):
    exp = set(daily.index[daily["is_expiry"] == True].strftime("%Y-%m-%d"))
    rows = []
    for date in sorted(days):
        d = days[date]
        base = smile(d, 15)
        s0 = d.spot[15]
        for m in CHECK:
            a = m - 1                                   # last completed bar
            now, prev = smile(d, a), smile(d, a - 30)
            rows.append({"date": date, "m": m, "expiry_day": date in exp,
                         "ret_day": math.log(d.spot[a] / s0), "dskew_day": now[1] - base[1],
                         "r30": math.log(d.spot[a] / d.spot[a - 30]),
                         "dcurv30": now[2] - prev[2], "datm30": now[0] - prev[0]})
    f = pd.DataFrame(rows)
    for c in ("ret_day", "dskew_day", "r30", "dcurv30", "datm30"):
        sd = f[c].shift(1).rolling(200, min_periods=100).std()
        f["z_" + c] = f[c] / sd
    f["alpha"] = f["z_ret_day"] - f["z_dskew_day"]
    same_day = f["date"].eq(f["date"].shift(1))
    f["mom"] = (f["alpha"] - f["alpha"].shift(1)).where(same_day)
    f["alpha2"] = f["z_r30"].where(f["dcurv30"] <= 0, 0.0)
    f["energy"] = f["z_r30"] ** 2 + f["z_datm30"] ** 2
    return f


def signal(f, th, gate, reverse=False):
    bull = (f["alpha"] > th) & (f["mom"] > 0) & (f["alpha2"] > 0)
    bear = (f["alpha"] < -th) & (f["mom"] < 0) & (f["alpha2"] < 0)
    if gate:
        bull &= f["energy"] >= 2
        bear &= f["energy"] >= 2
    side = pd.Series(0, index=f.index)
    side[bull], side[bear] = 1, -1
    side[f["expiry_day"]] = 0
    return -side if reverse else side


# --- simulation ----------------------------------------------------------------------------

def window_of(daily, days):
    """date -> (list of sessions from it to its weekly expiry)."""
    tds = [d for d in daily.index.strftime("%Y-%m-%d") if d in days]
    exp = set(daily.index[daily["is_expiry"] == True].strftime("%Y-%m-%d"))
    out, cur = {}, []
    for d in reversed(tds):
        if d in exp:
            cur = []
        cur = [d] + cur
        out[d] = list(cur)
    return out


def raw_ok(day, k, typ, m):
    j = day.col(k)
    return j is not None and not np.isnan((day.ce if typ == "CE" else day.pe)[m, j])


def trade(days, win, date, m, side, W, stop, hold):
    d0 = days[date]
    K = atm_strike(d0.spot[m])
    typ = "PE" if side > 0 else "CE"
    legs = [(typ, K, -1), (typ, K - W if side > 0 else K + W, +1)]
    if not all(raw_ok(d0, k, t, m) for t, k, _ in legs):
        return None
    sess = win[date][: hold + 1]
    paths, marks = [], []
    for t, k, _ in legs:
        p, ok = [], []
        for i, s in enumerate(sess):
            a, b = (m if i == 0 else 0), EXIT_MIN
            p.append(days[s].series(k, t, a, b))
            j = days[s].col(k)
            arr = (days[s].ce if t == "CE" else days[s].pe)
            ok.append(~np.isnan(arr[a:b + 1, j]) if j is not None else np.zeros(b - a + 1, bool))
        paths.append(np.concatenate(p))
        marks.append(np.concatenate(ok))
    day_ix = np.concatenate([[i] * ((EXIT_MIN - (m if i == 0 else 0)) + 1) for i in range(len(sess))])
    P = np.vstack(paths)
    sides = np.array([s for *_, s in legs])
    entry = np.array([px - slip(px) if s < 0 else px + slip(px) for px, s in zip(P[:, 0], sides)])
    credit = -(sides * entry).sum()
    if credit <= 0:
        return None
    mtm = (sides[:, None] * (P - entry[:, None])).sum(0)
    margin = W
    hit_t = np.nonzero(mtm >= 0.5 * credit)[0]
    hit_s = np.nonzero(mtm <= -stop * margin)[0]
    i, why = len(mtm) - 1, "time"
    for idx, w in ((hit_t, "target"), (hit_s, "stop")):
        if len(idx) and idx[0] < i:
            i, why = idx[0], w
    urgent = why == "stop"
    px = P[:, i]
    exitp = np.array([p + slip(p, urgent) if s < 0 else max(0.05, p - slip(p, urgent))
                      for p, s in zip(px, sides)])
    pts = (sides * (exitp - entry)).sum()
    fallback = not all(mk[i] for mk in marks)
    # daily marks for the account curve: end of each held session (or exit)
    ends = [np.nonzero(day_ix == k)[0][-1] for k in range(day_ix[i] + 1)]
    ends[-1] = i
    daily_pts = [(sess[day_ix[e]], float((sides * (P[:, e] - entry)).sum()) if e != i else float(pts))
                 for e in ends]
    exit_m = int(i - np.nonzero(day_ix == day_ix[i])[0][0] + (m if day_ix[i] == 0 else 0))
    return {"date": date, "m": m, "exit_date": sess[day_ix[i]], "exit_m": exit_m, "side": side, "W": W,
            "credit": credit, "pts": pts, "why": why, "fallback": fallback,
            "entry_px": entry.tolist(), "exit_px": exitp.tolist(), "marks": daily_pts}


def run(days, win, f, sides, W, stop, hold):
    out, busy_until = [], None
    for (date, m), s in zip(zip(f["date"], f["m"]), sides):
        if s == 0:
            continue
        if busy_until is not None and (date, m) <= busy_until:
            continue
        t = trade(days, win, date, m, s, W, stop, hold)
        if t is None:
            continue
        out.append(t)
        busy_until = (t["exit_date"], t["exit_m"])
    return pd.DataFrame(out)


def account(t, all_days, frac=0.25):
    """Lots = frac x equity / (W x lot) at entry (margin = W per unit); daily marked equity."""
    eq, pnl_day, rows = CAPITAL, {}, []
    for r in t.itertuples():
        if eq <= 0.1 * CAPITAL:                       # ruined: stop trading
            break
        lot = get_lot_size(r.date)
        lots = max(1, int(frac * eq // (r.W * lot)))
        qty = lots * lot
        cost = sum(option_fill_cost(a, qty, s > 0, r.date) + option_fill_cost(b, qty, s < 0, r.exit_date)
                   for a, b, s in zip(r.entry_px, r.exit_px, (-1, 1)))
        prev = 0.0
        for k, (dd, v) in enumerate(r.marks):
            x = (v - prev) * qty - (cost if k == len(r.marks) - 1 else 0)
            pnl_day[dd] = pnl_day.get(dd, 0.0) + x
            prev = v
        rs = r.pts * qty - cost
        rows.append({"date": r.date, "exit": r.exit_date, "rs": rs, "ret_margin": rs / (r.W * qty),
                     "ret_equity": rs / eq, "lots": lots})
        eq += rs
    s = pd.Series(pnl_day).reindex(all_days).fillna(0.0)
    e = CAPITAL + s.cumsum()
    r = s / e.shift(1).fillna(CAPITAL)
    return pd.DataFrame(rows), e, r, s


def period(t, all_days, a, z):
    """Fresh account of CAPITAL over [a, z): the trades entered in it, compounding."""
    sub = t[(t["date"] >= a) & (t["date"] < z)] if len(t) else t
    dd = [d for d in all_days if a <= d < z]
    tr, e, r, pnl = account(sub, dd)
    out = metrics(tr, e, r, pnl, a, z)
    if not len(sub):
        return out
    # per-trade statistics from every trade at one lot, so they are not cut off by ruin
    one = []
    for x in sub.itertuples():
        lot = get_lot_size(x.date)
        cost = sum(option_fill_cost(a_, lot, s_ > 0, x.date) + option_fill_cost(b_, lot, s_ < 0, x.exit_date)
                   for a_, b_, s_ in zip(x.entry_px, x.exit_px, (-1, 1))) / lot
        one.append((x.pts - cost) / x.W)
    m = pd.Series(one)
    won = m > 0
    streak = lambda b: max((len(list(g)) for k, g in itertools.groupby(b) if k), default=0)
    out.update({"trades_wk": len(m) / (len(dd) / 5), "win": won.mean(), "avg_win_m": m[won].mean(),
                "avg_loss_m": m[~won].mean(), "rr": -m[won].mean() / m[~won].mean(), "exp_m": m.mean(),
                "pf": m[won].sum() / -m[~won].sum(), "max_wins": streak(won.tolist()),
                "max_losses": streak((~won).tolist())})
    return out


def metrics(tr, e, r, pnl, a, z):
    tr = tr[(tr["date"] >= a) & (tr["date"] < z)]
    e, r, pnl = e.loc[a:z], r.loc[a:z], pnl.loc[a:z]
    e = e[e.index < z]
    r, pnl = r.loc[e.index], pnl.loc[e.index]
    if len(tr) == 0:
        return {}
    yrs = len(e) / 252
    start = e.iloc[0] / (1 + r.iloc[0])
    cagr = (e.iloc[-1] / start) ** (1 / yrs) - 1 if e.iloc[-1] > 0 else -1.0
    dd = e / e.cummax() - 1
    # recovery: calendar days from a peak to the next new high, per drawdown episode
    idx = pd.to_datetime(e.index)
    peaks, rec, peak_t = e.cummax(), [], None
    for t_, v, pk in zip(idx, e.to_numpy(), peaks.to_numpy()):
        if v < pk and peak_t is None:
            peak_t = prev_t
        if v >= pk and peak_t is not None:
            rec.append((t_ - peak_t).days)
            peak_t = None
        prev_t = t_
    won = tr["rs"] > 0
    streak = lambda b: max((len(list(g)) for k, g in itertools.groupby(b) if k), default=0)
    act = pnl[pnl != 0]
    ri = pd.Series(r.to_numpy(), index=idx)
    mo = (1 + ri).resample("ME").prod() - 1
    qu = (1 + ri).resample("QE").prod() - 1
    down = r[r < 0]
    return {"trades_wk": len(tr) / (len(e) / 5), "win": won.mean(),
            "avg_win_m": tr.loc[won, "ret_margin"].mean(), "avg_loss_m": tr.loc[~won, "ret_margin"].mean(),
            "rr": -tr.loc[won, "ret_margin"].mean() / tr.loc[~won, "ret_margin"].mean(),
            "exp_m": tr["ret_margin"].mean(), "cagr": cagr, "max_dd": dd.min(),
            "vol": r.std() * math.sqrt(252), "sharpe": r.mean() / r.std() * math.sqrt(252),
            "sortino": r.mean() / math.sqrt((down ** 2).sum() / len(r)) * math.sqrt(252),
            "calmar": cagr / -dd.min() if dd.min() < 0 else np.nan,
            "rec_avg": np.mean(rec) if rec else 0, "rec_max": max(rec) if rec else 0,
            "win_days": (act > 0).mean(), "win_months": (mo > 0).mean(), "win_q": (qu > 0).mean(),
            "max_wins": streak(won.tolist()), "max_losses": streak((~won).tolist()),
            "pf": tr.loc[won, "rs"].sum() / -tr.loc[~won, "rs"].sum()}


CLAIM = {"trades_wk": 2, "win": .5469, "avg_win_m": .0634, "avg_loss_m": -.0507, "rr": 1.25,
         "exp_m": .5469 * .0634 - .4531 * .0507, "cagr": 4.17 * .2912, "max_dd": -.2912,
         "vol": .6898, "sharpe": 1.89, "sortino": 3.18, "calmar": 4.17, "rec_avg": 11, "rec_max": 48,
         "win_days": .5614, "win_months": .6923, "win_q": .80, "max_wins": 7, "max_losses": 5}


def main():
    import os
    os.makedirs(OUT, exist_ok=True)
    days, daily = load_days(), load_daily()
    win = window_of(daily, days)
    f = features(days, daily)
    f.to_csv(f"{OUT}/features.csv", index=False)
    all_days = sorted(days)
    first = f.dropna(subset=["z_dskew_day"])["date"].min()
    all_days = [d for d in all_days if d >= first]
    f = f[f["date"] >= first].reset_index(drop=True)
    grid = list(itertools.product((0.5, 1.0), (100, 200), (0.3, 0.5), (0, 1), (False, True)))
    rows = []
    for th, W, stop, hold, gate in grid:
        t = run(days, win, f, signal(f, th, gate), W, stop, hold)
        key = (th, W, stop, hold, gate)
        d = period(t, all_days, first, SPLIT)
        o = period(t, all_days, SPLIT, "2027-01-01")
        rows.append({"th": th, "W": W, "stop": stop, "hold": hold, "gate": gate,
                     "fallback_exits": int(t["fallback"].sum()) if len(t) else 0,
                     **{"d_" + k: v for k, v in d.items()}, **{"o_" + k: v for k, v in o.items()}})
        print(key, "done", flush=True)
    g = pd.DataFrame(rows)
    g.to_csv(f"{OUT}/grid.csv", index=False)
    pd.set_option("display.width", 250)
    cols = ["th", "W", "stop", "hold", "gate", "fallback_exits", "d_trades_wk", "d_win", "d_exp_m", "d_sharpe",
            "d_cagr", "d_max_dd", "o_trades_wk", "o_win", "o_exp_m", "o_sharpe", "o_cagr", "o_max_dd"]
    print(g[cols].round(3).to_string(index=False))

    # frozen choice: best design profit per trade (share of margin; does not depend on sizing)
    # among cells with >= 1 trade a week in design
    ok = g[g["d_trades_wk"] >= 1]
    best = ok.loc[ok["d_exp_m"].idxmax()]
    key = (best.th, int(best.W), best.stop, int(best.hold), bool(best.gate))
    print("\nfrozen on design:", key)
    th, W, stop, hold, gate = key
    base = signal(f, th, gate)
    ctrl = {"rule": base, "reverse": -base,
            "always_bull": base.abs(), "always_bear": -base.abs()}
    out = {}
    for name, s in ctrl.items():
        t = run(days, win, f, s, W, stop, hold)
        out[name] = {"design": period(t, all_days, first, SPLIT),
                     "oos": period(t, all_days, SPLIT, "2027-01-01"),
                     "all": period(t, all_days, first, "2027-01-01")}
        if name == "rule":
            t.drop(columns=["marks"]).to_csv(f"{OUT}/frozen_trades.csv", index=False)
    # random direction at the rule's own entry checkpoints
    rng = np.random.default_rng(7)
    rnd = {"design": [], "oos": []}
    for _ in range(200):
        s = base.abs() * rng.choice([-1, 1], size=len(base))
        t = run(days, win, f, pd.Series(s, index=base.index), W, stop, hold)
        for per, (a, z) in (("design", (first, SPLIT)), ("oos", (SPLIT, "2027-01-01"))):
            sub = t[(t["date"] >= a) & (t["date"] < z)]
            rnd[per].append(sub["pts"].sum() / (W * len(sub)) if len(sub) else np.nan)
    tab = pd.DataFrame({"claim": CLAIM, **{f"{n}_{p}": out[n][p] for n in out for p in ("design", "oos", "all")}})
    tab.to_csv(f"{OUT}/frozen_vs_claim.csv")
    print(tab[["claim", "rule_design", "rule_oos", "rule_all"]].round(3).to_string())
    print(tab[[c for c in tab.columns if c.endswith("_oos") or c.endswith("_design")]].loc[
        ["trades_wk", "win", "exp_m", "sharpe", "cagr", "max_dd"]].round(3).to_string())
    for per in ("design", "oos"):
        x = np.array(rnd[per])
        t = run(days, win, f, base, W, stop, hold)
        a, z = (first, SPLIT) if per == "design" else (SPLIT, "2027-01-01")
        sub = t[(t["date"] >= a) & (t["date"] < z)]
        rs = sub["pts"].sum() / (W * len(sub))
        print(f"random direction {per}: median points per trade / W {np.nanmedian(x):+.3f}, "
              f"rule {rs:+.3f}, share of random >= rule {(x >= rs).mean():.2f}")


if __name__ == "__main__":
    main()
