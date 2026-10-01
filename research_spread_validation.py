"""
Validation of the two directional candidates that kept a consistent sign IS and OOS:
  pa_gap      : Nifty 09:30 vs previous close on the entry day (gap up -> bullish week)
  fii_opt_chg : day-before change in FII index-option net delta (long calls/short puts up -> bullish)

Every week is simulated once in BOTH directions, so any direction rule's P&L is a lookup.
Permutation test: 10,000 random direction assignments over the same weeks -> p-value of the
signal's P&L against pure chance (which already includes the credit spread's theta edge).
"""
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily
from research_directional_spreads import make_legs_fn

rp.QTY = 75


def both_directions(days, cyc, kind, stop, tp):
    rows = []
    for window, exp in cyc:
        e = window[0]
        r = {"entry": e}
        for d in (1, -1):
            x = rp.run_cycle(days, window, make_legs_fn(days[e], d, kind), stop, tp)
            r["bull" if d > 0 else "bear"] = x["rs"] if x else np.nan
        rows.append(r)
    return pd.DataFrame(rows).set_index("entry").dropna()


def perm_test(pnl, sig, n=10000, seed=1):
    x = pnl.join(sig.rename("s"), how="inner").dropna()
    x = x[x["s"] != 0]
    actual = np.where(x["s"] > 0, x["bull"], x["bear"]).sum()
    rng = np.random.default_rng(seed)
    choice = rng.integers(0, 2, size=(n, len(x))).astype(bool)
    rand = np.where(choice, x["bull"].to_numpy(), x["bear"].to_numpy()).sum(1)
    return len(x), actual, rand.mean(), (rand >= actual).mean()


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    days, daily = load_days(), load_daily()
    cyc = rp.cycles(daily, days)
    sig = pd.read_csv("data/reports/weekly_signals.csv", index_col="entry")
    out = []
    for kind, stop, tp in [("credit", None, None), ("credit", 1.5, None), ("debit", None, 1.0)]:
        tag = kind + (f"_sl{stop}" if stop else "") + (f"_tp{tp}" if tp else "")
        pnl = both_directions(days, cyc, kind, stop, tp)
        pnl.to_csv(f"data/reports/weekly_spread_pnl_{tag}.csv")
        for s in ["pa_gap", "fii_opt_chg", "pa_mom5", "pcr_entry", "fii_chg_z"]:
            for label, mask in [("IS", pnl.index < "2024-01-01"), ("OOS", pnl.index >= "2024-01-01"), ("ALL", pnl.index > "")]:
                n, act, rnd, p = perm_test(pnl[mask], np.sign(sig[s]))
                sub = pnl[mask].join(sig[s].rename("s")).dropna()
                sub = sub[sub.s != 0]
                out.append({"structure": tag, "signal": s, "set": label, "weeks": n, "signal_pnl": round(act),
                            "random_dir_avg": round(rnd), "always_bull": round(sub.bull.sum()),
                            "always_bear": round(sub.bear.sum()), "p_value": round(p, 4)})
        print("done", tag)
    res = pd.DataFrame(out)
    res.to_csv("data/reports/spread_validation.csv", index=False)
    print(res.to_string(index=False))

    # Year-by-year for the gap signal, credit spread no stop
    pnl = pd.read_csv("data/reports/weekly_spread_pnl_credit.csv", index_col="entry")
    x = pnl.join(np.sign(sig["pa_gap"]).rename("s")).dropna()
    x = x[x.s != 0]
    x["sig_pnl"] = np.where(x.s > 0, x.bull, x.bear)
    x["rand_exp"] = (x.bull + x.bear) / 2
    x["yr"] = x.index.str[:4]
    print("\nGap signal, credit spreads, by year:")
    print(x.groupby("yr")[["sig_pnl", "rand_exp", "bull", "bear"]].sum().round(0).assign(
        hit=x.groupby("yr").apply(lambda g: (g.sig_pnl > 0).mean().round(2))).to_string())
