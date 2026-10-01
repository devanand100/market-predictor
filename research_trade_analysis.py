"""
Trade-level diagnostics for Strategy 2 (confirmed credit spread).

Enriches every trade with: signal strengths, VIX / realised vol, short-strike cushion vs the
realised weekly move, max adverse excursion of spot toward the short strike, credit/width, and
whether the direction call was right. Also records the skipped weeks (signals disagreed).
Output: data/reports/spread_trades_enriched.csv, data/reports/spread_weeks_all.csv
"""
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily, atm_strike
from research_directional_spreads import signal_table, SPREAD_W

days, daily = load_days(), load_daily()
cyc = rp.cycles(daily, days)
sig = signal_table(daily, days, cyc)
led = pd.read_csv("data/reports/spread_strategy_trades.csv").set_index("entry")

rows = []
for window, exp in cyc:
    e = window[0]
    if e not in sig.index:
        continue
    s = sig.loc[e]
    d0 = days[e]
    spot0 = d0.spot[rp.ENTRY_MIN]
    K = atm_strike(spot0)
    S = d0.px(K, "CE", rp.ENTRY_MIN) + d0.px(K, "PE", rp.ENTRY_MIN)
    off = max(50, int(round(0.5 * S / 50)) * 50)
    # spot path entry -> expiry 15:15
    path = np.concatenate([days[w].spot[(rp.ENTRY_MIN if i == 0 else 0):(rp.EXIT_MIN + 1 if w == exp else 376)]
                           for i, w in enumerate(window)])
    spot1 = path[-1]
    ga, fb = np.sign(s["pa_gap"]), np.sign(s["fii_opt_chg"])
    traded = bool(ga == fb and fb != 0 and not np.isnan(fb))
    direction = int(ga) if traded else 0
    ts = pd.Timestamp(e)
    r = {"entry": e, "expiry": exp, "days_held": len(window), "expiry_dow": pd.Timestamp(exp).day_name()[:3],
         "traded": traded, "dir": direction, "spot": spot0, "straddle": S, "offset_pts": off,
         "offset_pct": off / spot0 * 100, "week_move_pct": (spot1 / spot0 - 1) * 100,
         "week_range_pct": (path.max() - path.min()) / spot0 * 100,
         "up_excursion_pct": (path.max() / spot0 - 1) * 100, "down_excursion_pct": (path.min() / spot0 - 1) * 100,
         "vix": daily.at[ts, "vix_0920"], "rv20": daily["rv20"].shift(1).get(ts, np.nan),
         "gap_pct": s["pa_gap"] * 100, "fii_opt_sig": s["fii_opt_chg"], "sig_gap": ga, "sig_fii": fb}
    r["implied_move_pct"] = S / spot0 * 100
    r["move_vs_implied"] = abs(r["week_move_pct"]) / r["implied_move_pct"]
    if traded:
        short_k = K - off if direction > 0 else K + off
        adverse = (path.min() - short_k) if direction > 0 else (short_k - path.max())   # <0 = breached
        r.update({"short_strike": short_k, "short_breached": adverse < 0,
                  "closest_to_short_pts": adverse, "exp_beyond_short": (spot1 < short_k) if direction > 0 else (spot1 > short_k),
                  "dir_right": np.sign(r["week_move_pct"]) == direction})
        if e in led.index:
            L = led.loc[e]
            r.update({"credit_pts": L["credit_pts"], "pnl_pts": L["pnl_pts"], "lots": L["lots"], "pnl_rs": L["pnl"],
                      "ret_pct": L["ret_pct"], "credit_over_width": L["credit_pts"] / SPREAD_W})
    else:
        r["bull_would_win"] = r["week_move_pct"] > -r["offset_pct"]
        r["bear_would_win"] = r["week_move_pct"] < r["offset_pct"]
    rows.append(r)

W = pd.DataFrame(rows)
W.to_csv("data/reports/spread_weeks_all.csv", index=False)
T = W[W.traded & W.ret_pct.notna()].copy()
T.to_csv("data/reports/spread_trades_enriched.csv", index=False)
print("weeks:", len(W), "traded:", len(T))
