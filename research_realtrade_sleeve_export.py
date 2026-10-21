"""
Each Strategy 6 sleeve on its own, Rs 3L account each, paper-bot settings (strategy6_bot/rules.py) and
Rs 590/month running costs, plus the combined portfolio for comparison. For the dashboard artifact.
Writes data/reports/strategy6_3L_sleeve_daily.json (date, sleeve, equity_mark) and
strategy6_3L_sleeve_trades.json (sleeve, entry, exit, pnl)
"""
import json
import sys

import research_realtrade as rt
from research_realtrade_portfolio import sleeves

sys.path.insert(0, "strategy6_bot")
import rules as R  # noqa: E402

START = 300_000
NAMES = {"S2": "Strangle 2 days out", "E0": "Expiry-day strangle", "GAP": "Gap buy"}

if __name__ == "__main__":
    rt.RUNNING = R.MONTHLY_RUNNING_COSTS
    W = rt.Weekly
    from research_realtrade_grid import strangle, long_dir
    cfg = [
        ("S2", [W("S2", strangle(1.25), dte=2, stop=1.0, vix_min=R.VIX_HI, mode="risk", weight=R.W_S2)], START, True),
        ("E0", [W("E0", strangle(0.5), dte=0, stop=0.5, vix_min=R.VIX_HI, mode="risk", weight=R.W_E0)], START, True),
        ("GAP", [W("GAP", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=R.VIX_LO, mode="risk",
                   weight=R.F_GAP)], START, True),
        ("ALL", sleeves(R.W_S2, R.W_E0, R.F_GAP, vix_hi=R.VIX_HI, vix_lo=R.VIX_LO), START, True),
    ]
    daily, trades = [], []
    for nm, (E, T, a) in rt.replay_many(cfg).items():
        label = "All three combined" if nm == "ALL" else NAMES[nm]
        daily += [{"date": r.date, "sleeve": label, "equity_mark": round(r.equity)} for r in E.itertuples()]
        trades += [{"sleeve": label, "entry": r.entry, "exit": r.exit, "pnl": round(r.pnl)}
                   for r in T[T.sleeve != "running"].itertuples()]
        rt.show(rt.report(label, E, T, a, START))
    for name, rows in (("sleeve_daily", daily), ("sleeve_trades", trades)):
        with open(f"data/reports/strategy6_3L_{name}.json", "w", encoding="utf-8") as fh:
            json.dump(rows, fh, separators=(",", ":"))
        print(name, len(rows))
