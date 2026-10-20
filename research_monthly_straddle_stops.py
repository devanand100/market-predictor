"""
Stop-loss and profit-target grid for the 45 -> 21 DTE monthly straddle (hold, no re-centring).

Both are fractions of the entry credit and are checked on every 60-minute bar close:
  target  exit when the profit after normal slippage reaches `tp` x credit
  stop    exit when the mark-to-market loss reaches `stop` x credit (urgent slippage on the exit)
A trade that hits neither closes at 21 DTE. After any exit the next entry is the next month's.
A stop can only act on an hourly close, so a gap through the level fills at the gapped price.

Each cell is reported with no entry filter and with the `vrp >= 2` filter, on the full period,
2021-2023 and 2024+.

Usage:  python research_monthly_straddle_stops.py
Writes: data/reports/monthly_straddle_stops_summary.csv
"""
import pandas as pd

import research_monthly_straddle as ms
import research_monthly_straddle_vix as mv
from engine import load_daily

TARGETS = [None, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
STOPS = [None, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00]


def main():
    chains, exps = ms.load_contracts()
    feats = mv.features(load_daily(), mv.load_vix())
    rows = []
    for tp in TARGETS:
        for stop in STOPS:
            t = ms.backtest(chains, exps, tp=tp, stop=stop)
            t["vrp"] = feats["vrp"].reindex(pd.to_datetime(t.entry), method="ffill").to_numpy()
            is_ = t.entry <= ms.IS_END
            spans = {"all": (t.entry.iloc[0], t.exit.iloc[-1]), "is": (t.entry.iloc[0], t[is_].exit.iloc[-1]),
                     "oos": (t[~is_].entry.iloc[0], t.exit.iloc[-1])}
            for filt, keep in (("none", t.vrp > -99), ("vrp >= 2", t.vrp >= 2)):
                k = t[keep]
                hold = (pd.to_datetime(k.exit) - pd.to_datetime(k.entry)).dt.days
                row = {"filter": filt, "tp": "none" if tp is None else tp, "stop": "none" if stop is None else stop,
                       "n": len(k), "n_tp": int((k.reason == "tp").sum()), "n_stop": int((k.reason == "stop").sum()),
                       "win_pct": round((k.pts > 0).mean() * 100, 1), "avg_pts": round(k.pts.mean(), 1),
                       "worst_pts": round(k.pts.min(), 1), "avg_hold_days": round(hold.mean(), 1)}
                for lab, m in (("all", t.entry > ""), ("is", is_), ("oos", ~is_)):
                    c, d = mv.span_cagr(t[keep & m], *spans[lab])
                    row.update({f"{lab}_cagr": c, f"{lab}_dd": d})
                rows.append(row)
        print(".", end="", flush=True)
    out = pd.DataFrame(rows)
    ms.OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(ms.OUT / "monthly_straddle_stops_summary.csv", index=False)

    pd.set_option("display.width", 220)
    order_tp = ["none" if x is None else x for x in TARGETS]
    order_st = ["none" if x is None else x for x in STOPS]
    for filt in ("none", "vrp >= 2"):
        o = out[out["filter"] == filt]
        for col, title in (("all_cagr", "CAGR % full period"), ("all_dd", "max drawdown % full period"),
                           ("is_cagr", "CAGR % 2021-2023"), ("oos_cagr", "CAGR % 2024+"),
                           ("n_stop", "trades stopped"), ("n_tp", "trades hitting target")):
            p = o.pivot(index="tp", columns="stop", values=col).reindex(index=order_tp, columns=order_st)
            print(f"\n[{filt}] {title}   (rows = target, columns = stop, both x credit)")
            print(p.to_string())


if __name__ == "__main__":
    main()
