"""
Family 3: Do FII / DII / Client / VIX signals predict Nifty moves (point-in-time)?

All participant OI values are lagged one session (NSE publishes after close), so a signal
dated D uses data from D-1 and is traded from D's open.
Targets: open->15:15 (intraday), prev close->open (gap, NOT tradable from signal), and
open(D) -> close(D+4) (5-day swing). Reports IS (2022-2023) vs OOS (2024-2026) rank IC
and quintile spreads, plus a futures long/short test net of costs.
"""
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from engine import load_daily
from cost_model import future_fill_cost, FUT_SLIP_PTS

d = load_daily()
d = d[d.index >= "2022-01-01"].copy()
d["y_intraday"] = d["close_1515"] / d["open"] - 1
d["y_swing5"] = d["close"].shift(-4) / d["open"] - 1
d["y_next_day"] = d["close"].shift(-1) / d["open"].shift(-1) - 1

d["sig_fii_chg"] = d["prev_fii_chg_z60"]
d["sig_fii_ls_z"] = d["prev_fii_ls_z60"]
d["sig_fii_opt_chg"] = d["prev_fii_idx_opt_chg"]
d["sig_client_net"] = -d["prev_client_idx_fut_net"]        # contrarian to retail
d["sig_fii_minus_client"] = d["prev_fii_idx_fut_net"] - d["prev_client_idx_fut_net"]
d["sig_vix_chg"] = -d["vix_close"].pct_change().shift(1)    # yesterday's VIX fall = bullish?
d["sig_vrp"] = d["vix_open"] - d["rv20"].shift(1)
d["sig_mom5"] = d["close"].pct_change(5).shift(1)
d["sig_rev1"] = -d["ret"].shift(1)

sigs = [c for c in d.columns if c.startswith("sig_")]
rows = []
for s in sigs:
    for y in ["y_intraday", "y_swing5"]:
        for name, sub in [("IS", d[d.index < "2024-01-01"]), ("OOS", d[d.index >= "2024-01-01"])]:
            x = sub[[s, y]].dropna()
            if len(x) < 50:
                continue
            ic = spearmanr(x[s], x[y]).correlation
            q = pd.qcut(x[s].rank(method="first"), 5, labels=False)
            spread = (x[y][q == 4].mean() - x[y][q == 0].mean()) * 1e4
            rows.append({"signal": s, "target": y, "set": name, "n": len(x), "rankIC": round(ic, 3),
                         "Q5-Q1_bps": round(spread, 1)})
res = pd.DataFrame(rows).pivot_table(index=["signal", "target"], columns="set", values=["rankIC", "Q5-Q1_bps"])
print(res.round(3).to_string())

# Tradable test: long/short 1 lot-equivalent (75u) Nifty future open->15:15 on sign of FII change z
print("\nFutures test: trade sign(prev FII fut change z) when |z|>1, open->15:15, net of costs")
for name, sub in [("IS", d[d.index < "2024-01-01"]), ("OOS", d[d.index >= "2024-01-01"])]:
    x = sub.dropna(subset=["sig_fii_chg"])
    x = x[x["sig_fii_chg"].abs() > 1]
    side = np.sign(x["sig_fii_chg"])
    pts = side * (x["close_1515"] - x["open"]) - 2 * FUT_SLIP_PTS
    cost = [future_fill_cost(p, 75, True, str(i.date())) + future_fill_cost(p, 75, False, str(i.date()))
            for i, p in x["open"].items()]
    rs = pts * 75 - np.array(cost)
    print(f"  {name}: trades={len(rs)} net=Rs{rs.sum():,.0f} avg=Rs{rs.mean():,.0f} win={100*(rs>0).mean():.1f}%")
res.to_csv("data/reports/research_signals.csv")
