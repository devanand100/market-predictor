"""
Sized backtests of the two ideas that survived research_vix_regime.py diagnostics:
  A. Reward/risk entry filter for Strategy 2: skip a trade if credit / (200 - credit) < threshold.
  B. Squeeze breakout: when recent ranges are compressed (atr_ratio in 2021-23 bottom tercile),
     BUY the ATM straddle at the weekly entry and hold to expiry (or take profit), i.e. try to
     monetise the "quiet then explode" effect with long options instead of avoiding it.
"""
import numpy as np
import pandas as pd

import research_positional as rp
import final_strategy as fs
import final_spread_strategy as f2
from engine import load_days, load_daily, atm_strike
from research_vix_regime import attach, terciles


def compounded(df):
    r = (1 + df.ret_pct / 100).cumprod()
    return (r.iloc[-1] - 1) * 100 if len(r) else 0.0, ((r / r.cummax()) - 1).min() * 100 if len(r) else 0.0


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    # ---------- A. Reward/risk filter on Strategy 2 ----------
    df, daily = f2.spread_trades()
    df["rr"] = df["credit"] / (200 - df["credit"])
    print("=== A. Strategy 2 with minimum reward/risk (credit / max loss) ===")
    rows = []
    for thr in [0.0, 0.18, 0.20, 0.232, 0.25, 0.28, 0.305]:
        sub = df[df.rr >= thr]
        led = f2.simulate_spreads(sub)
        m = fs.metrics(led)
        out = {"min_rr": thr, "min_credit_pts": round(200 * thr / (1 + thr), 1), "trades": m["trades"], "cagr": m["cagr_pct"],
               "maxDD": m["max_dd_pct"], "sharpe": m["sharpe_weekly_ann"], "win%": m["win_rate_pct"]}
        for lab, a, b in [("IS 22-23", "2000", "2023-12-31"), ("OOS 24-26", "2024-01-01", "2100"), ("Dec25-Apr26", "2025-12-01", "2026-04-30")]:
            ret, dd = compounded(led[(led.entry >= a) & (led.entry <= b)])
            out[lab] = f"{ret:+.1f}% / DD {dd:.1f}%"
        rows.append(out)
    print(pd.DataFrame(rows).to_string(index=False))

    # Same filter combined with bear-calls-only
    print("\nBear calls only, with and without the reward/risk filter:")
    for thr in [0.0, 0.232]:
        sub = df[(df.rr >= thr) & (df.dir == "bear call")]
        m = fs.metrics(f2.simulate_spreads(sub))
        print(f"  min_rr={thr}: trades={m['trades']} CAGR={m['cagr_pct']}% DD={m['max_dd_pct']}% Sharpe={m['sharpe_weekly_ann']}")

    # ---------- B. Squeeze -> buy straddle ----------
    print("\n=== B. Buy ATM straddle at weekly entry, by range-compression regime ===")
    rp.QTY = 65
    days = load_days()
    cyc = rp.cycles(daily, days)
    W = attach(pd.DataFrame({"entry": [w[0] for w, e in cyc]}))
    W["_d"] = 0
    cuts = terciles(W, "atr_ratio")
    squeeze = set(W[W.atr_ratio < cuts[0]].entry)
    print("atr_ratio squeeze cut (2021-23 bottom tercile):", round(cuts[0], 3))

    def straddle(spot):
        K = atm_strike(spot)
        return [("CE", K, +1), ("PE", K, +1)]

    res = []
    for tp in [None, 0.3, 0.5, 1.0]:
        trades = []
        for window, exp in cyc:
            r = rp.run_cycle(days, window, straddle, None, tp)
            if r:
                r["squeeze"] = window[0] in squeeze
                trades.append(r)
        T = pd.DataFrame(trades)
        T["per"] = np.where(T.entry < "2024-01-01", "IS 21-23", "OOS 24-26")
        g = T.groupby(["squeeze", "per"])["rs"].agg(n="size", total="sum", avg="mean", win=lambda s: (s > 0).mean() * 100).round(0)
        g["tp"] = tp
        res.append(g)
    print(pd.concat(res).to_string())
