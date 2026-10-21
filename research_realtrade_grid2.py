"""
Refinement of the structures that survived research_realtrade_grid.py (1 lot each, real prices):
strangle 2 sessions before expiry, expiry-day strangle/straddle, gap buy. Filters: VIX band at 09:20,
lagged VRP (VIX - previous session RV20), width, stop, target, check interval. Select on IS only.
Writes data/reports/realtrade_grid2.csv
"""
import pandas as pd
import research_realtrade as rt
from research_realtrade_grid import strangle, straddle, long_dir, summarize

W = rt.Weekly


def candidates():
    c = []
    for mult in (0.75, 1.0, 1.25):
        for stop in (0.75, 1.0, 1.5):
            c.append(W(f"S2 m{mult} sl{stop}", strangle(mult), dte=2, stop=stop))
    for vmin, vmax in ((None, 13), (None, 15), (13, None), (15, None), (12, 20), (None, 18)):
        c.append(W(f"S2 m1.0 sl1.0 vix{vmin}-{vmax}", strangle(1.0), dte=2, stop=1.0, vix_min=vmin, vix_max=vmax))
    for vr in (-2, 0, 2, 4):
        c.append(W(f"S2 m1.0 sl1.0 vrp>={vr}", strangle(1.0), dte=2, stop=1.0, vrp_min=vr))
    for tp in (0.5, 0.7):
        c.append(W(f"S2 m1.0 sl1.0 tp{tp}", strangle(1.0), dte=2, stop=1.0, tp=tp))
    c.append(W("S2 m1.0 sl1.0 chk5", strangle(1.0), dte=2, stop=1.0, check_every=5))
    c.append(W("S2 m1.0 sl1.0 in10:15", strangle(1.0), dte=2, stop=1.0, m_in=60))
    c.append(W("S1 m1.0 sl1.0 exit dte0", strangle(1.0), dte=1, stop=1.0))
    c.append(W("S3to1 m1.0 sl1.0", strangle(1.0), dte=3, stop=1.0, exit_dte=1))
    # expiry day
    for mult in (0.25, 0.5, 0.75):
        for stop in (0.5, 0.75, 1.0):
            c.append(W(f"E0 m{mult} sl{stop}", strangle(mult), dte=0, stop=stop))
    for vmin, vmax in ((None, 13), (None, 15), (13, None), (15, None)):
        c.append(W(f"E0 m0.5 sl1.0 vix{vmin}-{vmax}", strangle(0.5), dte=0, stop=1.0, vix_min=vmin, vix_max=vmax))
    for vr in (0, 2):
        c.append(W(f"E0 m0.5 sl1.0 vrp>={vr}", strangle(0.5), dte=0, stop=1.0, vrp_min=vr))
    for tp in (0.5, 0.7):
        c.append(W(f"E0 m0.5 sl1.0 tp{tp}", strangle(0.5), dte=0, stop=1.0, tp=tp))
    c.append(W("E0 m0.5 sl1.0 in10:15", strangle(0.5), dte=0, stop=1.0, m_in=60))
    c.append(W("E0 m0.5 sl1.0 in11:15", strangle(0.5), dte=0, stop=1.0, m_in=120))
    c.append(W("E0 straddle sl0.5 in10:15", straddle(), dte=0, stop=0.5, m_in=60))
    # gap buy
    for v in (12, 13, 14, 15, 16, 18):
        c.append(W(f"GAP dte4 tp1.0 vix<={v}", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=v))
    for g in (0.002, 0.004):
        c.append(W(f"GAP dte4 tp1.0 vix<=15 gap>{g}", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True,
                   vix_max=15, min_gap=g))
    for tp in (0.75, 1.5):
        c.append(W(f"GAP dte4 tp{tp} vix<=15", long_dir(0), dte=4, direction="gap", tp=tp, tp_limit=True, vix_max=15))
    c.append(W("GAP dte4 tp1.0 vrp<=0", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vrp_max=0))
    c.append(W("GAP dte4 tp1.0 vix<=15 vrp<=2", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=15, vrp_max=2))
    return c


if __name__ == "__main__":
    import sys
    part, nparts = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (0, 1)
    C = candidates()[part::nparts]
    print(len(C), "candidates", flush=True)
    rows = []
    for k in range(0, len(C), 20):
        res = rt.replay_many([(c.sleeve, [c], 1e8, False) for c in C[k:k + 20]])
        for nm, (E, T, a) in res.items():
            r = summarize(nm, T)
            r["unobs"] = a.unobserved
            rows.append(r)
        print(part, k + 20, "done", flush=True)
        pd.DataFrame(rows).to_csv(f"data/reports/realtrade_grid2_{part}.csv", index=False)
