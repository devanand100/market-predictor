"""
Grid 3: the surviving structures with VIX filters on the PREVIOUS session's VIX close (complete for
2021, known before the open). 1 lot each on real prices, select on IS (2021-23).
Writes data/reports/realtrade_grid3.csv (parts _0.._2 when run in parallel)
"""
import sys
import pandas as pd
import research_realtrade as rt
from research_realtrade_grid import strangle, long_dir, summarize

W = rt.Weekly


def candidates():
    c = []
    bands = [(None, None), (13, None), (15, None), (17, None), (12, 22), (None, 20), (None, 15)]
    for mult in (1.0, 1.25):
        for stop in (0.75, 1.0):
            for vmin, vmax in bands:
                c.append(W(f"S2 m{mult} sl{stop} vix{vmin}-{vmax}", strangle(mult), dte=2, stop=stop,
                           vix_min=vmin, vix_max=vmax))
    for mult in (0.25, 0.5):
        for stop in (0.5, 1.0):
            for vmin, vmax in [(None, None), (13, None), (15, None), (None, 20)]:
                c.append(W(f"E0 m{mult} sl{stop} vix{vmin}-{vmax}", strangle(mult), dte=0, stop=stop,
                           vix_min=vmin, vix_max=vmax))
    for v in (None, 13, 14, 15, 16, 18, 20):
        c.append(W(f"GAP tp1.0 vix<={v}", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=v))
    for g in (0.002, 0.004):
        c.append(W(f"GAP tp1.0 vix<=15 gap>{g}", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True,
                   vix_max=15, min_gap=g))
    return c


if __name__ == "__main__":
    part, nparts = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (0, 1)
    C = candidates()[part::nparts]
    rows = []
    for k in range(0, len(C), 20):
        res = rt.replay_many([(c.sleeve, [c], 1e8, False) for c in C[k:k + 20]])
        for nm, (E, T, a) in res.items():
            r = summarize(nm, T)
            r["unobs"] = a.unobserved
            rows.append(r)
        print(part, k + 20, "done", flush=True)
        pd.DataFrame(rows).to_csv(f"data/reports/realtrade_grid3_{part}.csv", index=False)
