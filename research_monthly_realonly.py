"""
Monthly 45->21 DTE short straddle (Strategy 5's MON sleeve) priced on REAL prices only.

research_monthly_straddle.leg_price fills a strike with no traded price by put-call parity or Black-76
(26 of 69 exits). Here such a bar uses the last real price of that contract, moved by the change in
intrinsic value since then (never below current intrinsic): no parity, no model. The exit pays urgent
slippage when a leg is stale. Same entry/exit rules, VRP >= 2 filter as Strategy 5.

Writes data/reports/monthly_realonly_trades.csv (both pricings side by side, per lot of 65)
"""
import pandas as pd

import research_monthly_straddle as ms
import research_monthly_straddle_vix as mv
from engine import load_daily

_last = {}
_orig = ms.leg_price


def leg_price_real(chain, ts, expiry, K, typ):
    q = chain.px.get((K, typ))
    S = chain.spot
    intr = max(S - K, 0.0) if typ == "CE" else max(K - S, 0.0)
    key = (expiry, K, typ)
    if q is not None:
        _last[key] = (q, intr)
        return q, "real"
    if key not in _last:
        return None, None                     # no real price seen yet: bar skipped
    p0, i0 = _last[key]
    return max(p0 + (intr - i0), intr, 0.05), "model"   # 'model' tag -> urgent slippage at exit


def run(real_only):
    ms.leg_price = leg_price_real if real_only else _orig
    _last.clear()
    chains, exps = ms.load_contracts()
    t = ms.backtest(chains, exps)
    ms.leg_price = _orig
    return t


if __name__ == "__main__":
    a, b = run(False), run(True)
    f = mv.features(load_daily(), mv.load_vix())
    feat = f.reindex(pd.to_datetime(a.entry), method="ffill").reset_index(drop=True)
    a = pd.concat([a, feat[["vrp"]]], axis=1)
    m = a[["expiry", "entry", "exit", "strike", "spot", "credit", "pts", "rs", "px_ce", "px_pe", "vrp"]].merge(
        b[["expiry", "pts", "rs", "worst_pts", "mtm"]], on="expiry", suffixes=("_research", "_real"))
    m["synthetic_exit"] = (m.px_ce != "real") | (m.px_pe != "real")
    m.drop(columns="mtm").to_csv("data/reports/monthly_realonly_trades.csv", index=False)
    m.to_pickle("data/reports/monthly_realonly_trades.pkl")
    k = m[m.vrp >= 2]
    pf = lambda x: round(x[x > 0].sum() / -x[x < 0].sum(), 2)
    for nm, g in (("all", m), ("VRP>=2 (Strategy 5)", k)):
        print(f"{nm}: n {len(g)}, synthetic exits {int(g.synthetic_exit.sum())} | research PF {pf(g.rs_research)} "
              f"Rs/lot {g.rs_research.sum():,.0f} | real-only PF {pf(g.rs_real)} Rs/lot {g.rs_real.sum():,.0f}")
        s = g[g.synthetic_exit]
        print(f"   synthetic-exit trades: research Rs/lot {s.rs_research.sum():,.0f}, real-only {s.rs_real.sum():,.0f}")
