"""
VIX / reward-risk entry filters and "compression -> expansion" (squeeze) regimes.

Features (all known at entry, 09:30 on the first session after expiry):
  vix          : VIX at 09:20
  vix_pct252   : VIX percentile vs the previous 252 sessions (0 = 1-year low)
  vix_chg5     : VIX vs 5 sessions ago (rising fear)
  rv_ratio     : RV5 / RV20 (short-term realised vol vs longer; < 1 = calming)
  atr_ratio    : mean(high-low, 5d) / mean(high-low, 60d)  (< 1 = compressed ranges)
  bbw_pct126   : Bollinger bandwidth(20) percentile vs previous 126 sessions (low = squeeze)
  iv_rv        : VIX / RV20 (options rich vs realised)
Outcome per week: |move| / implied move (ATM straddle at entry). > 1 = the week "exploded"
beyond what options priced -> bad for sellers, good for buyers.

Thresholds are terciles computed on 2021-2023 ONLY, then applied unchanged to 2024-2026.
"""
import numpy as np
import pandas as pd

from engine import load_daily

d = load_daily()
c, h, l = d["close"], d["high"], d["low"]
f = pd.DataFrame(index=d.index)
f["vix"] = d["vix_0920"]
vc = d["vix_close"]
f["vix_pct252"] = vc.shift(1).rolling(252, min_periods=120).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
f["vix_chg5"] = vc.shift(1) / vc.shift(6) - 1
r = np.log(c).diff()
f["rv_ratio"] = (r.rolling(5).std() / r.rolling(20).std()).shift(1)
rng = (h - l) / c
f["atr_ratio"] = (rng.rolling(5).mean() / rng.rolling(60).mean()).shift(1)
bbw = 4 * c.rolling(20).std() / c.rolling(20).mean()
f["bbw_pct126"] = bbw.shift(1).rolling(126, min_periods=60).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)
f["iv_rv"] = d["vix_0920"] / d["rv20"].shift(1)
FEATS = ["vix", "vix_pct252", "vix_chg5", "rv_ratio", "atr_ratio", "bbw_pct126", "iv_rv"]


def attach(df, key="entry"):
    x = df.copy()
    idx = pd.to_datetime(x[key])
    for k in FEATS:
        x[k] = f[k].reindex(idx).to_numpy()
    return x


def terciles(x, k):
    """Cut points from 2021-2023 only."""
    base = x[x.entry < "2024-01-01"][k].dropna()
    return base.quantile([1 / 3, 2 / 3]).to_numpy()


def bucket(x, k, cuts):
    return pd.cut(x[k], [-np.inf, cuts[0], cuts[1], np.inf], labels=["low", "mid", "high"])


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    W = attach(pd.read_csv("data/reports/spread_weeks_all.csv"))
    W["per"] = np.where(W.entry < "2024-01-01", "21-23", "24-26")
    W["explode"] = W["move_vs_implied"] > 1.0
    print("=== 1. Do these features predict weeks that move MORE than options priced? (all 295 weeks) ===")
    print("Cells: share of weeks with |move| > implied move, and mean |move|/implied. Terciles fixed on 2021-23.")
    for k in FEATS:
        cuts = terciles(W, k)
        W["b"] = bucket(W, k, cuts)
        t = W.groupby(["b", "per"], observed=True).agg(explode=("explode", "mean"), ratio=("move_vs_implied", "mean"), n=("explode", "size"))
        t = t.unstack("per").round(2)
        print(f"\n{k}  (cuts {np.round(cuts, 3)})")
        print(t.to_string())

    # ---- 2. Filters on the two strategies
    S1 = attach(pd.read_csv("data/reports/final_trades.csv"))
    S2 = attach(pd.read_csv("data/reports/spread_trades_enriched.csv"))
    S2["rr"] = S2["credit_pts"] / (200 - S2["credit_pts"])
    print("\n=== 2. Entry filters: avg return per trade by tercile (IS 21/22-23 | OOS 24-26) ===")
    for name, S in [("Strategy 1 strangle", S1), ("Strategy 2 spread", S2)]:
        print(f"\n##### {name}")
        feats = FEATS + (["rr"] if name.startswith("Strategy 2") else [])
        for k in feats:
            cuts = terciles(S, k)
            S["b"] = bucket(S, k, cuts)
            S["per"] = np.where(S.entry < "2024-01-01", "IS", "OOS")
            t = S.groupby(["b", "per"], observed=True)["ret_pct"].agg(["size", "mean"]).unstack("per").round(2)
            print(f"{k:11s} cuts={np.round(cuts, 3)}\n{t.to_string()}")
