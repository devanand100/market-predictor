"""
Re-centring the 45 -> 21 DTE monthly straddle (research_monthly_straddle.py).

Rule: while the trade is open, if the forward has moved `trigger` away from the short strike, buy
back both legs and sell a new straddle at the strike now nearest the forward (same expiry). The
cycle still closes at 21 DTE. Lots are fixed for the whole cycle; every re-centre adds 4 orders.

Configs:
  trigger   ("pct", x)     x % of the current short strike
            ("credit", m)  m x the credit of the first straddle, in points (1.0 = its breakeven)
            ("ratio", r)   the cheaper leg is worth r x the dearer leg or less (0.5 = half)
  max_adj   maximum re-centres per cycle (0 = the plain hold, None = unlimited)
  check     "hourly" (every 60-minute bar) or "eod" (15:15 bar only)

Each config is reported with no entry filter and with the straddle's `vrp >= 2` filter, on the
full period, 2021-2023 and 2024+.

Usage:  python research_monthly_straddle_recenter.py
Writes: data/reports/monthly_straddle_recenter_summary.csv
"""
from datetime import date

import numpy as np
import pandas as pd

import research_monthly_straddle as ms
import research_monthly_straddle_vix as mv
from cost_model import option_fill_cost, slip
from engine import load_daily

LOT = ms.LOT
TRIGGERS = [("pct", 1.0), ("pct", 1.5), ("pct", 2.0), ("pct", 2.5), ("pct", 3.0), ("credit", 0.5), ("credit", 1.0),
            ("ratio", 0.5), ("ratio", 0.33), ("ratio", 0.25)]
LABEL = {"pct": "{:g}%", "credit": "{:g}x credit", "ratio": "leg ratio {:g}"}


def atm_both(c):
    """Strike nearest the forward with real quotes on both legs."""
    both = [k for (k, t) in c.px if t == "CE" and (k, "PE") in c.px]
    return min(both, key=lambda k: abs(k - c.forward())) if both and c.forward() is not None else None


def run_cycle(chain, expiry, entry_date, exit_date, trigger=None, max_adj=0, check="hourly"):
    bars = [ts for ts in chain if str(ts.date()) >= entry_date]
    entry = K = None
    for ts in bars:
        if str(ts.date()) > entry_date:
            break
        if ts.strftime("%H:%M") >= ms.ENTRY_TIME:
            K = atm_both(chain[ts])
            if K is not None:
                entry = ts
                break
    if entry is None:
        return None
    c = chain[entry]
    spot0, K0, d0 = c.spot, K, str(entry.date())
    ce, pe = c.px[(K, "CE")], c.px[(K, "PE")]
    open_credit = first_credit = (ce - slip(ce)) + (pe - slip(pe))
    cost = option_fill_cost(ce, LOT, False, d0) + option_fill_cost(pe, LOT, False, d0)
    realised, n_adj, mtm, worst = 0.0, 0, {}, 0.0

    for ts in bars:
        if ts <= entry:
            continue
        c = chain[ts]
        (pc, hc), (pp, hp) = ms.leg_price(c, ts, expiry, K, "CE"), ms.leg_price(c, ts, expiry, K, "PE")
        if pc is None or pp is None:
            continue
        d, hm = str(ts.date()), ts.strftime("%H:%M")
        pnl_mid = realised + open_credit - pc - pp
        mtm[d] = pnl_mid
        worst = min(worst, pnl_mid)
        close = sum(p + slip(p, h == "model") for p, h in ((pc, hc), (pp, hp)))
        buy_cost = option_fill_cost(pc, LOT, True, d) + option_fill_cost(pp, LOT, True, d)
        if d >= exit_date and hm >= ms.ENTRY_TIME:
            pts = realised + open_credit - close
            cost += buy_cost
            mtm[d] = pts
            return {"expiry": expiry, "entry": d0, "exit": d, "reason": "time", "strike": K0, "spot": spot0,
                    "fwd": 0.0, "entry_dte": (date.fromisoformat(expiry) - date.fromisoformat(d0)).days,
                    "exit_dte": (date.fromisoformat(expiry) - date.fromisoformat(d)).days, "exit_spot": c.spot,
                    "credit": first_credit, "pts": pts, "worst_pts": worst, "px_ce": hc, "px_pe": hp,
                    "cost": cost, "rs": pts * LOT - cost, "mtm": mtm, "n_adj": n_adj}
        if trigger is None or (max_adj is not None and n_adj >= max_adj) or (check == "eod" and hm != "15:15"):
            continue
        F = c.forward()
        new_k = atm_both(c)
        if F is None or new_k is None or new_k == K:
            continue
        if trigger[0] == "ratio":
            hit = min(pc, pp) <= trigger[1] * max(pc, pp)
        else:
            hit = abs(F - K) >= (trigger[1] / 100 * K if trigger[0] == "pct" else trigger[1] * first_credit)
        if not hit:
            continue
        realised += open_credit - close                 # buy back the old straddle
        ce, pe = c.px[(new_k, "CE")], c.px[(new_k, "PE")]
        open_credit = (ce - slip(ce)) + (pe - slip(pe))  # sell the new one at the forward
        cost += buy_cost + option_fill_cost(ce, LOT, False, d) + option_fill_cost(pe, LOT, False, d)
        K, n_adj = new_k, n_adj + 1
        mtm[d] = realised + open_credit - ce - pe
        worst = min(worst, mtm[d])
    return None


def backtest(chains, exps, **cfg):
    rows = []
    for e in exps:
        if e not in chains:
            continue
        sessions = sorted({str(ts.date()) for ts in chains[e]})
        dte = lambda d: (date.fromisoformat(e) - date.fromisoformat(d)).days
        exit_date = next((d for d in sessions if dte(d) <= ms.EXIT_DTE), None)
        entry_date = next((d for d in sessions if dte(d) <= ms.ENTRY_DTE), None)
        if entry_date and exit_date and dte(entry_date) >= ms.ENTRY_DTE - 4:
            t = run_cycle(chains[e], e, entry_date, exit_date, **cfg)
            if t:
                rows.append(t)
    return pd.DataFrame(rows)


def main():
    chains, exps = ms.load_contracts()
    feats = mv.features(load_daily(), mv.load_vix())
    configs = [dict(trigger=None, max_adj=0, check="hourly")]
    configs += [dict(trigger=tr, max_adj=m, check=ch) for tr in TRIGGERS for m in (1, None) for ch in ("hourly", "eod")]
    rows = []
    for cfg in configs:
        t = backtest(chains, exps, **cfg)
        t["vrp"] = feats["vrp"].reindex(pd.to_datetime(t.entry), method="ffill").to_numpy()
        is_ = t.entry <= ms.IS_END
        spans = {"all": (t.entry.iloc[0], t.exit.iloc[-1]), "is": (t.entry.iloc[0], t[is_].exit.iloc[-1]),
                 "oos": (t[~is_].entry.iloc[0], t.exit.iloc[-1])}
        for filt, keep in (("none", t.vrp > -99), ("vrp >= 2", t.vrp >= 2)):
            k = t[keep]
            row = {"trigger": "hold" if cfg["trigger"] is None else LABEL[cfg["trigger"][0]].format(cfg["trigger"][1]),
                   "max_adj": "any" if cfg["max_adj"] is None else cfg["max_adj"], "check": cfg["check"], "filter": filt,
                   "n": len(k), "adj_per_trade": round(k.n_adj.mean(), 2), "adj_trades_pct": round((k.n_adj > 0).mean() * 100),
                   "win_pct": round((k.pts > 0).mean() * 100, 1), "avg_pts": round(k.pts.mean(), 1),
                   "worst_pts": round(k.pts.min(), 1), "total_pts": round(k.pts.sum())}
            for lab, m in (("all", t.entry > ""), ("is", is_), ("oos", ~is_)):
                c, d = mv.span_cagr(t[keep & m], *spans[lab])
                row.update({f"{lab}_cagr": c, f"{lab}_dd": d})
            rows.append(row)
        print(".", end="", flush=True)
    out = pd.DataFrame(rows)
    ms.OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(ms.OUT / "monthly_straddle_recenter_summary.csv", index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_rows", 200)
    for filt in ("none", "vrp >= 2"):
        print(f"\n=== entry filter: {filt} ===")
        print(out[out["filter"] == filt].drop(columns="filter").to_string(index=False))


if __name__ == "__main__":
    main()
