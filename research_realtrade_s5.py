"""
Strategy 5 (three_sleeve_bot) on the real-market replay: how much of its edge is synthetic.

Sleeve ladders (1 account per variant, Rs 10 crore so lot rounding does not matter, no running costs):
  L2  bot rules: tranche stop 1.5x checked every 5 min (TICK), >= 3 lots
  GAP bot rules: VIX(09:20) <= 15, target = 5-min close >= 2x fill, filled at that close
Run once with the old engine's intrinsic fallback for missing prices (`synthetic`) and once on real prices
only (`real`), each in its own process because the switch is module-wide:
  python research_realtrade_s5.py synthetic | real
Writes data/reports/realtrade_s5_<mode>.csv (trades of every variant)
"""
import sys
import pandas as pd
import research_realtrade as rt

if __name__ == "__main__":
    mode = sys.argv[1]
    rt.SYNTHETIC_FALLBACK = mode == "synthetic"
    cfg = [
        ("L2 bot 5-min", [rt.StaggeredStrangle(0.30, stop=1.5, check_every=5, min_total=3)]),
        ("L2 1-min", [rt.StaggeredStrangle(0.30, stop=1.5, check_every=1, min_total=3)]),
        ("GAP bot close-target", [rt.GapBuy(0.03, vix_max=15, target="close", vix_src="0920", check_every=5)]),
        ("GAP limit-target", [rt.GapBuy(0.03, vix_max=15, target="limit", vix_src="0920")]),
    ]
    res = rt.replay_many([(n, s, 1e8, False) for n, s in cfg])
    out = []
    for n, (E, T, a) in res.items():
        T = T.copy()
        T["variant"], T["unobs_total"] = n, a.unobserved
        out.append(T)
    pd.concat(out).to_csv(f"data/reports/realtrade_s5_{mode}.csv", index=False)
    print(mode, "finished", flush=True)
