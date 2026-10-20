"""
VIX entry filters for the 45 -> 21 DTE monthly straddle (research_monthly_straddle.py).

Every feature is taken from the session BEFORE the entry day (no look-ahead):
  vix        India VIX close
  ivr        percentile rank of that close within the prior 252 sessions (0-100)
  vrp        VIX - 20-day realised vol
  vix_ma10   VIX / its 10-session mean - 1   (> 0 = volatility expanding)

A filter skips the whole monthly cycle; capital sits idle. Thresholds are judged on 2021-2023
entries, 2024+ is out-of-sample. Each filter is also compared with skipping the same number of
trades at random (`rand_pctl` = share of 1,000 random skips the filter beats on CAGR / drawdown).

Usage:  python research_monthly_straddle_vix.py [--refresh]     # --refresh re-downloads daily VIX from Dhan
Writes: data/reports/monthly_straddle_vix_filters.csv, monthly_straddle_vix_trades.csv
"""
import sys

import numpy as np
import pandas as pd

import research_monthly_straddle as ms
from engine import load_daily

N_RANDOM = 1000
VIX_CACHE = ms.ROOT / "data" / "raw" / "india_vix_daily_dhan.csv"

FILTERS = {
    "none": lambda t: t.vix > 0,
    "vix >= 12": lambda t: t.vix >= 12,
    "vix >= 13": lambda t: t.vix >= 13,
    "vix >= 14": lambda t: t.vix >= 14,
    "vix >= 15": lambda t: t.vix >= 15,
    "vix <= 16": lambda t: t.vix <= 16,
    "vix <= 18": lambda t: t.vix <= 18,
    "vix <= 20": lambda t: t.vix <= 20,
    "vix <= 22": lambda t: t.vix <= 22,
    "12 <= vix <= 20": lambda t: (t.vix >= 12) & (t.vix <= 20),
    "13 <= vix <= 22": lambda t: (t.vix >= 13) & (t.vix <= 22),
    "ivr >= 25": lambda t: t.ivr >= 25,
    "ivr >= 50": lambda t: t.ivr >= 50,
    "ivr <= 50": lambda t: t.ivr <= 50,
    "ivr <= 75": lambda t: t.ivr <= 75,
    "vrp >= 0": lambda t: t.vrp >= 0,
    "vrp >= 1": lambda t: t.vrp >= 1,
    "vrp >= 2": lambda t: t.vrp >= 2,
    "vrp >= 3": lambda t: t.vrp >= 3,
    "vrp >= 4": lambda t: t.vrp >= 4,
    "vix falling (<= 10d mean)": lambda t: t.vix_ma10 <= 0,
    "vix rising (> 10d mean)": lambda t: t.vix_ma10 > 0,
    "vix not spiking (<= +10%)": lambda t: t.vix_ma10 <= 0.10,
}


def load_vix(refresh=False):
    """Daily India VIX closes from mid-2019 (daily_features only has VIX from Aug 2021).
    Cached in data/raw/; a refresh needs a valid Dhan token."""
    if VIX_CACHE.exists() and not refresh:
        s = pd.read_csv(VIX_CACHE, index_col="date", parse_dates=True)["close"]
    else:
        import update_market_data as u
        dhan = u.dhan_client()
        r = dhan.historical_daily_data(security_id="21", exchange_segment=dhan.INDEX, instrument_type="INDEX",
                                       from_date="2019-06-01", to_date=str(pd.Timestamp.today().date()))
        if r.get("status") != "success":
            raise SystemExit(f"Dhan VIX download failed: {r.get('remarks')}")
        d = pd.DataFrame(r["data"])
        d["date"] = (pd.to_datetime(d["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)).dt.normalize()
        s = d.drop_duplicates("date").set_index("date")["close"].sort_index()
        s.to_csv(VIX_CACHE)
    return s


def features(daily, v):
    f = pd.DataFrame({
        "vix": v,
        "ivr": v.rolling(252, min_periods=200).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True),
        "vrp": v - daily["rv20"].reindex(v.index, method="ffill"),   # VIX has special sessions daily lacks
        "vix_ma10": v / v.rolling(10).mean() - 1,
    })
    return f.shift(1)                                   # known before the entry session opens


def quick(df):
    """CAGR and daily mark-to-market drawdown for a subset of trades (same sizing as the base run)."""
    if len(df) < 3:
        return np.nan, np.nan
    led, curve = ms.simulate(df)
    m = ms.metrics(led, curve)
    return m["cagr_pct"], m["max_dd_pct"]


def span_cagr(df, start, end):
    """CAGR over a fixed calendar span, so skipped months count as idle time."""
    if len(df) == 0:
        return 0.0, 0.0
    led, curve = ms.simulate(df)
    yrs = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    eq = pd.concat([pd.Series([ms.START_CAPITAL]), curve]).reset_index(drop=True)
    final = eq.iloc[-1]
    cagr = ((final / ms.START_CAPITAL) ** (1 / yrs) - 1) * 100 if final > 0 else -100.0
    return round(float(cagr), 1), round(float((eq / eq.cummax() - 1).min() * 100), 1)


def describe(sub):
    if len(sub) == 0:
        return {"n": 0}
    w = sub[sub.pts > 0].pts.sum()
    l = -sub[sub.pts <= 0].pts.sum()
    return {"n": len(sub), "win_pct": round((sub.pts > 0).mean() * 100, 1), "avg_pts": round(sub.pts.mean(), 1),
            "pf": round(w / l, 2) if l > 0 else np.inf, "worst_pts": round(sub.pts.min(), 1)}


def main():
    chains, exps = ms.load_contracts()
    trades = ms.backtest(chains, exps)
    f = features(load_daily(), load_vix("--refresh" in sys.argv))
    feat = f.reindex(pd.to_datetime(trades.entry), method="ffill").reset_index(drop=True)
    t = pd.concat([trades, feat], axis=1)
    is_ = t.entry <= ms.IS_END
    full = (t.entry.iloc[0], t.exit.iloc[-1])
    spans = {"is": (t.entry.iloc[0], t[is_].exit.iloc[-1]), "oos": (t[~is_].entry.iloc[0], t.exit.iloc[-1])}

    print("Entry-day feature vs trade P&L (points), 2021-2023 only:")
    for col, bins in (("vix", [0, 12, 14, 16, 20, 99]), ("ivr", [-1, 25, 50, 75, 101]),
                      ("vrp", [-99, 0, 2, 4, 99]), ("vix_ma10", [-9, -0.05, 0, 0.05, 9])):
        g = t[is_].groupby(pd.cut(t[is_][col], bins), observed=True).pts
        print(f"  {col}: " + " | ".join(f"{k}: n={n}, avg {a:.0f}, win {w:.0%}" for k, n, a, w in
                                         zip(g.size().index, g.size(), g.mean(), g.apply(lambda s: (s > 0).mean()))))
    print("  Spearman corr with P&L (IS / OOS):",
          {c: (round(t[is_][c].corr(t[is_].pts, method="spearman"), 2),
               round(t[~is_][c].corr(t[~is_].pts, method="spearman"), 2)) for c in f.columns})

    rng = np.random.default_rng(7)
    rows = []
    for name, fn in FILTERS.items():
        keep = fn(t).fillna(False).to_numpy()
        k = t[keep]
        cagr, dd = span_cagr(k, *full)
        row = {"filter": name, "kept": int(keep.sum()), "skipped": int((~keep).sum()), "cagr_pct": cagr, "max_dd_pct": dd,
               **{f"all_{a}": b for a, b in describe(k).items() if a != "n"}}
        for lab, mask in (("is", is_), ("oos", ~is_)):
            sub = t[keep & mask.to_numpy()]
            c, d = span_cagr(sub, *spans[lab])
            row.update({f"{lab}_n": len(sub), f"{lab}_cagr_pct": c, f"{lab}_max_dd_pct": d,
                        f"{lab}_avg_pts": describe(sub).get("avg_pts"), f"{lab}_win_pct": describe(sub).get("win_pct")})
        if 0 < keep.sum() < len(t):
            rc, rd = [], []
            for _ in range(N_RANDOM):
                idx = np.sort(rng.choice(len(t), int(keep.sum()), replace=False))
                c, d = span_cagr(t.iloc[idx], *full)
                rc.append(c)
                rd.append(d)
            row["rand_pctl_cagr"] = round(float((np.array(rc) < cagr).mean() * 100))
            row["rand_pctl_dd"] = round(float((np.array(rd) < dd).mean() * 100))
        rows.append(row)
        print(".", end="", flush=True)
    out = pd.DataFrame(rows)
    ms.OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(ms.OUT / "monthly_straddle_vix_filters.csv", index=False)
    t.drop(columns="mtm").to_csv(ms.OUT / "monthly_straddle_vix_trades.csv", index=False)
    pd.set_option("display.width", 260)
    print("\n" + out[["filter", "kept", "skipped", "cagr_pct", "max_dd_pct", "all_win_pct", "all_pf",
                      "is_n", "is_cagr_pct", "is_max_dd_pct", "oos_n", "oos_cagr_pct", "oos_max_dd_pct",
                      "rand_pctl_cagr", "rand_pctl_dd"]].to_string(index=False))


if __name__ == "__main__":
    main()
