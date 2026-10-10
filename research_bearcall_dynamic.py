"""
Do the bear-call rules need to scale with the market level?

Static pieces in the current rules:
  1. Spread width fixed at 200 points (1.16% of spot in 2022, 0.82% in 2025-26).
  2. FII signal centred on the EXPANDING median of past weeks (anchored to 2022 contract counts,
     although the lot size changed 50 -> 25 -> 75 -> 65 and with it the number of contracts).
Tested here (bear-call weeks only, credit filter credit/(W - credit) >= 0.232, 8% risk at max loss W x 65):
  - width: fixed 150/200/250/300 points vs 0.8%/1.0%/1.2% of spot rounded to 50
  - FII centring: expanding median vs rolling 52-week median vs rolling 26-week median
Long legs must stay inside Dhan's +/-500-point window at entry, else the trade is flagged (the far leg
would be priced at intrinsic, which flatters the trade). Choose on 2022-23, check on 2024-26.
"""
import numpy as np
import pandas as pd

import research_etf_overlay as ov
import research_positional as rp
from engine import atm_strike, load_daily, load_days
from research_directional_spreads import signal_table

LOT = 65


def bear_legs(day, width_fn):
    def f(spot):
        K = atm_strike(spot)
        S = day.px(K, "CE", rp.ENTRY_MIN) + day.px(K, "PE", rp.ENTRY_MIN)
        d = max(50, int(round(0.5 * S / 50)) * 50)
        W = width_fn(spot)
        return [("CE", K + d, -1), ("CE", K + d + W, +1)]
    return f


def run(days, daily, entries, width_fn):
    rows = []
    for window, exp in rp.cycles(daily, days):
        e = window[0]
        if e not in entries:
            continue
        spot = days[e].spot[rp.ENTRY_MIN]
        W = width_fn(spot)
        legs = bear_legs(days[e], width_fn)(spot)
        r = rp.run_cycle(days, window, bear_legs(days[e], width_fn), None, None)
        if not r or r["credit"] / (W - r["credit"]) < ov.RR_MIN:
            continue
        r.update(W=W, long_outside=(legs[1][1] - atm_strike(spot)) > 500)
        rows.append(r)
    return pd.DataFrame(rows)


def sized(t, start=200_000, risk=ov.RISK):
    eq, out = float(start), []
    for r in t.itertuples():
        lots = max(1, int(risk * eq // (r.W * LOT)))
        eq += r.rs * lots
        out.append((r.exit, eq))
    return pd.Series(dict(out))


def summary(t, label):
    rows = []
    for per, g in (("2022-23", t[t.entry < "2024"]), ("2024-26", t[t.entry >= "2024"]), ("all", t)):
        if g.empty:
            continue
        eq = sized(g)
        yrs = (pd.Timestamp(g.exit.iloc[-1]) - pd.Timestamp(g.entry.iloc[0])).days / 365.25
        rows.append({"variant": label, "period": per, "n": len(g), "win%": (g.rs > 0).mean() * 100,
                     "avg_rs_per_lot": g.rs.mean(), "worst_rs_per_lot": g.rs.min(),
                     "cagr_2L": ((eq.iloc[-1] / 200_000) ** (1 / yrs) - 1) * 100,
                     "dd_2L": (eq / eq.cummax() - 1).min() * 100,
                     "long_outside_window": int(g.long_outside.sum())})
    return rows


def main():
    rp.QTY = LOT
    days, daily = load_days(), load_daily()
    sig = signal_table(daily, days, rp.cycles(daily, days))
    raw = daily["prev_fii_idx_opt_chg"].reindex(pd.to_datetime(sig.index)).to_numpy()
    raw = pd.Series(raw, index=sig.index)
    gap = np.sign(sig["pa_gap"])
    centred = {
        "expanding median": raw - raw.expanding(min_periods=20).median().shift(1),
        "rolling 52w median": raw - raw.rolling(52, min_periods=20).median().shift(1),
        "rolling 26w median": raw - raw.rolling(26, min_periods=20).median().shift(1),
    }
    r50 = lambda x: int(round(x / 50)) * 50
    widths = {"fixed 200 (current)": lambda s: 200, "fixed 150": lambda s: 150, "fixed 250": lambda s: 250,
              "fixed 300": lambda s: 300, "0.8% of spot": lambda s: max(100, r50(0.008 * s)),
              "1.0% of spot": lambda s: max(100, r50(0.010 * s)), "1.2% of spot": lambda s: max(100, r50(0.012 * s))}

    rows = []
    base_entries = set(sig.index[(gap < 0) & (np.sign(centred["expanding median"]) < 0)])
    for name, fn in widths.items():
        rows += summary(run(days, daily, base_entries, fn), f"width: {name}")
    for name, c in centred.items():
        if name == "expanding median":
            continue
        ent = set(sig.index[(gap < 0) & (np.sign(c) < 0)])
        rows += summary(run(days, daily, ent, widths["fixed 200 (current)"]), f"FII: {name}")
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/bearcall_dynamic_summary.csv", index=False)
    pd.set_option("display.width", 220)
    print(res.round(1).to_string(index=False))


if __name__ == "__main__":
    main()
