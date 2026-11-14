"""Survivor final runs: selected config, raw config, slippage x2/x3, trader-like exits; Sensibull-style accuracy.
Writes data/reports/survivor_final.json, survivor_best_*.csv, survivor_raw_equity.csv. Run: python research_survivor_final.py"""
import sys, json, numpy as np, pandas as pd
sys.path.insert(0, '.')
import research_survivor as rs, research_realtrade as rr, cost_model
rs._patch_data()
allday = (0,1,2,3,4,5,6)
BEST = dict(gap=30, sgap=200, reset=90, clip_w=0.04, spot_stop=75, max_open=6, entry_dte=allday)
RAW = dict(gap=30, sgap=200, reset=90, clip_w=0.04, entry_dte=allday)
def acc(E, T, start=1_000_000):
    tr = T[T.sleeve != "running"]
    rd = tr.groupby("exit").pnl.sum()                 # realised P&L by exit day (Sensibull-style)
    e = np.concatenate([[start], E.equity.to_numpy()]); dm = np.diff(e); dm = dm[np.abs(dm) > 1]
    return dict(trade_win=round(100*(tr.pnl > 0).mean(), 1), realised_day_win=round(100*(rd > 0).mean(), 1),
                mtm_day_win=round(100*(dm > 0).mean(), 1), realised_days=len(rd))
res = {}
def go(tag, kw, slipx=1.0):
    orig = rr.slip
    rr.slip = lambda p, urgent=False: slipx * orig(p, urgent)
    out = rr.replay_many([(tag, [rs.Survivor(tag, **kw)], 1_000_000, True)])
    rr.slip = orig
    E, T, a = out[tag]
    r = rr.report(tag, E, T, a, 1_000_000); r.update(acc(E, T)); r.update(rs.day_stats(E, 1_000_000))
    tr = T[T.sleeve != "running"]
    r["reasons"] = tr.reason.value_counts().to_dict()
    r["avg_win"] = round(tr[tr.pnl > 0].pnl.mean()); r["avg_loss"] = round(tr[tr.pnl < 0].pnl.mean())
    r["months_pos"] = round(100 * (E.assign(m=E.date.str[:7]).groupby("m").equity.last().pct_change().fillna(E.equity.iloc[0]/1e6-1) > 0).mean(), 1)
    res[tag] = r
    print(tag, {k: r[k] for k in ("cagr","max_dd","pf_trades","trades","is_cagr","is_dd","is_pf","oos_cagr","oos_dd","oos_pf",
          "trade_win","realised_day_win","mtm_day_win","worst_day","avg_win","avg_loss","months_pos","years","reasons")}, flush=True)
    return E, T
E, T = go("best", BEST)
E.to_csv("data/reports/survivor_best_equity.csv", index=False); T.to_csv("data/reports/survivor_best_trades.csv", index=False)
Er, Tr = go("raw", RAW)
Er.to_csv("data/reports/survivor_raw_equity.csv", index=False)
go("best slip x2", BEST, 2.0)
go("best slip x3", BEST, 3.0)
# trader-style: hold losers, book winners at 50% (realised accuracy inflator)
go("hold+tp50 (trader-like)", dict(RAW, tp=0.5))
json.dump(res, open("data/reports/survivor_final.json", "w"), default=str, indent=1)
