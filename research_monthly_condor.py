"""
Monthly iron condor / iron fly, entered near 45 DTE and closed at 21 DTE, on 60-minute data.
Same contracts, pricing ladder and exit timing as research_monthly_straddle.py.

What the data can and cannot test. Dhan stores only ATM +/-10 strikes (+/-500 points of spot) for
the next-month contract, and +/-3 from 2026. A textbook condor (16-delta shorts) sits about
1,100 points out at 45 DTE, so its ENTRY prices do not exist in the data.
  real-entry structures   all four legs have real quotes at entry (so entries end in 2025):
      fly_fwd_300         short straddle at the forward strike, wings +/-300
      ic_fwd_100_300      short forward +/-100, long forward +/-300
      ic_spot_300_500     short spot ATM +/-300, long spot ATM +/-500
  model-entry structure   ESTIMATE ONLY, entry credit comes from extrapolated IV:
      ic_16d_w300         short forward +/- 1.25 x straddle (about 16 delta), wings 300 beyond,
                          priced twice: skew extrapolated linearly, and flat from the edge strike

Sizing is by defined risk: lots = RISK of equity / max loss per lot, capped at MAX_LOTS.
Statutory costs are computed on the real order quantity (8 orders per trade).
The `vrp >= 2` rows apply the entry filter found for the straddle, unchanged.

Usage:  python research_monthly_condor.py
Writes: data/reports/monthly_condor_summary.csv, monthly_condor_trades.csv
"""
from datetime import date

import numpy as np
import pandas as pd

import research_monthly_straddle as ms
import research_monthly_straddle_vix as mv
from cost_model import option_fill_cost, slip
from engine import load_daily

RISK = 0.10               # share of equity lost if a trade hits its maximum loss
MAX_LOTS = 20
LOT = ms.LOT


def _r(x, step=50):
    return int(round(x / step) * step)


def fly_fwd_300(c, ts, e):
    k = _r(c.forward())
    return [(k, "CE", -1), (k, "PE", -1), (k + 300, "CE", 1), (k - 300, "PE", 1)]


def ic_fwd_100_300(c, ts, e):
    k = _r(c.forward())
    return [(k + 100, "CE", -1), (k - 100, "PE", -1), (k + 300, "CE", 1), (k - 300, "PE", 1)]


def ic_spot_300_500(c, ts, e):
    k = _r(c.spot)
    return [(k + 300, "CE", -1), (k - 300, "PE", -1), (k + 500, "CE", 1), (k - 500, "PE", 1)]


def ic_16d_w300(c, ts, e):
    k = _r(c.forward())
    ce, pe = c.px.get((k, "CE")), c.px.get((k, "PE"))
    if ce is None or pe is None:
        return None
    d = _r(1.25 * (ce + pe), 100)
    return [(k + d, "CE", -1), (k - d, "PE", -1), (k + d + 300, "CE", 1), (k - d - 300, "PE", 1)]


STRUCTURES = {  # name -> (legs function, entry must be real quotes)
    "fly_fwd_300": (fly_fwd_300, True),
    "ic_fwd_100_300": (ic_fwd_100_300, True),
    "ic_spot_300_500": (ic_spot_300_500, True),
    "ic_16d_w300": (ic_16d_w300, False),
}


def run_trade(chain, expiry, entry_date, exit_date, legs_fn, real_entry, tp=None):
    bars = [ts for ts in chain if str(ts.date()) >= entry_date]
    entry = legs = None
    for ts in bars:
        if str(ts.date()) > entry_date:
            break
        c = chain[ts]
        if ts.strftime("%H:%M") < ms.ENTRY_TIME or c.forward() is None:
            continue
        cand = legs_fn(c, ts, expiry)
        if cand is None:
            continue
        priced = [ms.leg_price(c, ts, expiry, k, t) for k, t, _ in cand]
        if any(p is None for p, _ in priced) or (real_entry and any(h != "real" for _, h in priced)):
            continue
        entry, legs, p_in, how_in = ts, cand, [p for p, _ in priced], [h for _, h in priced]
        break
    if entry is None:
        return None
    c = chain[entry]
    credit = sum((p - slip(p)) if s < 0 else -(p + slip(p)) for (_, _, s), p in zip(legs, p_in))
    width = max(abs(legs[2][0] - legs[0][0]), abs(legs[1][0] - legs[3][0]))
    if credit <= 0 or credit >= width:
        return None                                     # crossed or stale quotes: not a tradable condor
    d0 = str(entry.date())
    mtm, worst = {}, 0.0
    for ts in bars:
        if ts <= entry:
            continue
        c1 = chain[ts]
        priced = [ms.leg_price(c1, ts, expiry, k, t) for k, t, _ in legs]
        if any(p is None for p, _ in priced):
            continue
        d = str(ts.date())
        pnl_mid = credit + sum(s * p for (_, _, s), (p, _) in zip(legs, priced))
        mtm[d] = pnl_mid
        worst = min(worst, pnl_mid)
        reason = None
        if d >= exit_date and ts.strftime("%H:%M") >= ms.ENTRY_TIME:
            reason = "time"
        elif tp is not None and pnl_mid - sum(slip(p) for p, _ in priced) >= tp * credit:
            reason = "tp"
        if reason:
            p_out = [max(p + slip(p, h == "model"), 0.05) if s < 0 else max(p - slip(p, h == "model"), 0.0)
                     for (_, _, s), (p, h) in zip(legs, priced)]
            pts = credit + sum(s * p for (_, _, s), p in zip(legs, p_out))
            pts = max(pts, credit - width)              # a condor cannot lose more than width - credit
            mtm[d] = pts
            modelled = any(h == "model" for _, h in priced)
            return {"expiry": expiry, "entry": d0, "exit": d, "reason": reason,
                    "strikes": "/".join(str(k) for k, _, _ in legs), "spot": c.spot, "exit_spot": c1.spot,
                    "entry_dte": (date.fromisoformat(expiry) - date.fromisoformat(d0)).days,
                    "width": width, "credit": credit, "max_loss": width - credit, "pts": pts, "worst_pts": worst,
                    "entry_model": any(h == "model" for h in how_in), "exit_model": modelled,
                    "legs": [(s, pi, po) for (_, _, s), pi, po in zip(legs, p_in, p_out)], "mtm": mtm}
    return None


def backtest(chains, exps, legs_fn, real_entry, tp=None):
    rows = []
    for e in exps:
        if e not in chains:
            continue
        sessions = sorted({str(ts.date()) for ts in chains[e]})
        dte = lambda d: (date.fromisoformat(e) - date.fromisoformat(d)).days
        exit_date = next((d for d in sessions if dte(d) <= ms.EXIT_DTE), None)
        entry_date = next((d for d in sessions if dte(d) <= ms.ENTRY_DTE), None)
        if entry_date and exit_date and dte(entry_date) >= ms.ENTRY_DTE - 4:
            t = run_trade(chains[e], e, entry_date, exit_date, legs_fn, real_entry, tp)
            if t:
                rows.append(t)
    return pd.DataFrame(rows)


def simulate(df, start=ms.START_CAPITAL):
    eq, last_month, rows, curve = start, None, [], {}
    for r in df.itertuples():
        month = pd.Period(r.entry[:7], "M")
        fixed = ms.RUNNING_PER_MONTH * (1 if last_month is None else (month - last_month).n)
        last_month = month
        lots = min(MAX_LOTS, int(RISK * eq // (max(r.max_loss, 0.25 * r.width) * LOT)))
        qty = lots * LOT
        cost = 0.0 if lots == 0 else sum(option_fill_cost(pi, qty, s > 0, r.entry) + option_fill_cost(po, qty, s < 0, r.exit)
                                         for s, pi, po in r.legs)
        for d, pts in r.mtm.items():
            curve[d] = eq + qty * pts
        pnl = r.pts * qty - cost - fixed
        how = "model" if r.exit_model else "real"
        rows.append({"expiry": r.expiry, "entry": r.entry, "exit": r.exit, "reason": r.reason, "strikes": r.strikes,
                     "spot": round(r.spot, 1), "exit_spot": round(r.exit_spot, 1), "lots": lots, "width": r.width,
                     "credit_pts": round(r.credit, 1), "max_loss_pts": round(r.max_loss, 1), "pnl_pts": round(r.pts, 1),
                     "worst_mtm_pts": round(r.worst_pts, 1), "entry_model": r.entry_model, "px_ce": how, "px_pe": how,
                     "stat_costs": round(cost), "fixed_costs": fixed, "pnl": round(pnl),
                     "ret_pct": pnl / eq * 100, "equity": round(eq + pnl)})
        eq += pnl
        curve[r.exit] = eq
    return pd.DataFrame(rows), pd.Series(curve).sort_index()


def perf(df, start, end):
    """Stats over a fixed calendar span, so skipped months count as idle time."""
    if len(df) < 3:
        return {"n": len(df)}
    led, curve = simulate(df)
    yrs = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    eq = pd.concat([pd.Series([ms.START_CAPITAL]), curve]).reset_index(drop=True)
    final = eq.iloc[-1]
    w, l = led[led.pnl > 0].pnl.sum(), -led[led.pnl <= 0].pnl.sum()
    return {"n": len(led), "cagr_pct": round(float((final / ms.START_CAPITAL) ** (1 / yrs) - 1) * 100, 1) if final > 0 else -100.0,
            "max_dd_pct": round(float((eq / eq.cummax() - 1).min() * 100), 1),
            "win_pct": round(float((led.pnl > 0).mean() * 100), 1), "pf": round(float(w / l), 2) if l > 0 else np.inf,
            "avg_credit": round(float(led.credit_pts.mean()), 1), "avg_max_loss": round(float(led.max_loss_pts.mean()), 1),
            "avg_pts": round(float(led.pnl_pts.mean()), 1), "worst_pct": round(float(led.ret_pct.min()), 1),
            "avg_lots": round(float(led.lots.mean()), 1), "costs": int(led.stat_costs.sum())}


def main():
    chains, exps = ms.load_contracts()
    feats = mv.features(load_daily(), mv.load_vix())
    rows, keep_trades = [], []
    for name, (fn, real_entry) in STRUCTURES.items():
        for skew in ((True,) if real_entry else (True, False)):
            ms.LINEAR_SKEW = skew
            for tp in (None, 0.5):
                t = backtest(chains, exps, fn, real_entry, tp)
                if len(t) == 0:
                    continue
                t["vrp"] = feats["vrp"].reindex(pd.to_datetime(t.entry), method="ffill").to_numpy()
                is_ = t.entry <= ms.IS_END
                label = name + ("" if real_entry else (" [model, linear skew]" if skew else " [model, flat skew]"))
                for filt, keep in (("none", t.vrp > -99), ("vrp >= 2", t.vrp >= 2)):
                    row = {"structure": label, "tp": tp, "filter": filt,
                           **perf(t[keep], t.entry.iloc[0], t.exit.iloc[-1])}
                    for lab, m in (("is", is_), ("oos", ~is_)):
                        sub, span = t[keep & m], t[m]
                        if len(span):
                            p = perf(sub, span.entry.iloc[0], span.exit.iloc[-1])
                            row.update({f"{lab}_n": p["n"], f"{lab}_cagr_pct": p.get("cagr_pct"), f"{lab}_max_dd_pct": p.get("max_dd_pct")})
                    rows.append(row)
                if tp is None:
                    led, _ = simulate(t)
                    keep_trades.append(led.assign(structure=label, vrp=t.vrp.to_numpy()))
    ms.LINEAR_SKEW = True
    out = pd.DataFrame(rows)
    ms.OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(ms.OUT / "monthly_condor_summary.csv", index=False)
    pd.concat(keep_trades).to_csv(ms.OUT / "monthly_condor_trades.csv", index=False)
    pd.set_option("display.width", 280)
    pd.set_option("display.max_columns", 40)
    print(out[["structure", "tp", "filter", "n", "cagr_pct", "max_dd_pct", "win_pct", "pf", "avg_credit", "avg_max_loss",
               "avg_pts", "worst_pct", "avg_lots", "is_cagr_pct", "is_max_dd_pct", "oos_n", "oos_cagr_pct", "oos_max_dd_pct"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
