"""
Family 4: Directional weekly spreads driven by NSE participant data, PCR, and price-action trend.

For every weekly cycle (entry 09:30 on the session after expiry, exit expiry-day 15:15) we build
point-in-time signals, measure how well each predicts the week's Nifty move (IS 2021/22-2023 vs
OOS 2024-2026), and trade each signal with vertical spreads, contract-exact:
  credit : bull -> sell PE at ATM - d, buy PE 200 lower ; bear -> sell CE at ATM + d, buy CE 200 higher
           d = 0.5 x ATM straddle (rounded to 50, min 50)
  debit  : bull -> buy ATM CE, sell ATM+200 CE ;          bear -> buy ATM PE, sell ATM-200 PE
Baselines: always-bull and always-bear with the same structure. A signal only has value if it
beats BOTH baselines, out-of-sample.
Sign convention for every signal is fixed a priori from market lore (documented below), not fitted.
"""
import itertools
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

import research_positional as rp
from engine import load_days, load_daily, atm_strike, DATA

SPREAD_W = 200


def pcr_table():
    """OI put-call ratio over the stored +/-10-strike window of the nearest weekly contract."""
    o = pd.read_parquet(DATA / "options_1min.parquet", columns=["date", "minute", "ce_oi", "pe_oi"])
    g = o.groupby(["date", "minute"])[["ce_oi", "pe_oi"]].sum()
    g["pcr"] = g["pe_oi"] / g["ce_oi"]
    g = g.reset_index()
    at_0929 = g[g.minute == 14].set_index("date")["pcr"].rename("pcr_0929")
    last = g.sort_values("minute").groupby("date").last()["pcr"].rename("pcr_close")
    return pd.concat([at_0929, last], axis=1)


def signal_table(daily, days, cyc):
    pcr = pcr_table()
    c = daily["close"]
    ema20 = c.ewm(span=20, adjust=False).mean()
    rows = []
    for window, exp in cyc:
        e = window[0]
        ts = pd.Timestamp(e)
        i = daily.index.get_loc(ts)
        if i < 25:
            continue
        prev = daily.index[i - 1]
        d0 = days[e]
        s_entry = d0.spot[rp.ENTRY_MIN]
        s_exit = days[exp].spot[rp.EXIT_MIN]
        hi5 = daily["high"].iloc[i - 5:i].max()
        lo5 = daily["low"].iloc[i - 5:i].min()
        r = {
            "entry": e, "expiry": exp, "y": s_exit / s_entry - 1,
            # ---- price action (trend-following: + = bullish)
            "pa_mom5": c.iloc[i - 1] / c.iloc[i - 6] - 1,
            "pa_mom20": c.iloc[i - 1] / c.iloc[i - 21] - 1,
            "pa_ema20_pos": c.iloc[i - 1] / ema20.iloc[i - 1] - 1,
            "pa_ema20_slope": ema20.iloc[i - 1] / ema20.iloc[i - 6] - 1,
            "pa_range_pos": (c.iloc[i - 1] - lo5) / max(hi5 - lo5, 1) - 0.5,
            "pa_gap": s_entry / c.iloc[i - 1] - 1,
            # ---- VIX: falling VIX = bullish
            "vix_trend": -(daily["vix_close"].iloc[i - 1] / daily["vix_close"].iloc[i - 6] - 1),
            # ---- PCR (lore: high PCR = heavy put writing = support = bullish)
            "pcr_entry": pcr["pcr_0929"].get(e, np.nan),
            "pcr_prevday": pcr["pcr_close"].get(prev.strftime("%Y-%m-%d"), np.nan),
            # ---- NSE participant OI (already lagged 1 day in daily): FII long build-up = bullish
            "fii_chg_z": daily["prev_fii_chg_z60"].iloc[i],
            "fii_ls_z": daily["prev_fii_ls_z60"].iloc[i],
            "fii_flow5": daily["prev_fii_idx_fut_chg"].iloc[i - 4:i + 1].sum(),
            "fii_opt_chg": daily["prev_fii_idx_opt_chg"].iloc[i],
            # Clients are the classic contrarian indicator: client net long = bearish
            "client_contra": -daily["prev_client_idx_fut_net"].iloc[i],
            "pro_net": daily["prev_pro_idx_fut_net"].iloc[i],
        }
        rows.append(r)
    df = pd.DataFrame(rows).set_index("entry")
    # Centre level-type signals point-in-time (expanding median of PAST values only)
    for col in ["pcr_entry", "pcr_prevday", "client_contra", "pro_net", "fii_flow5", "fii_opt_chg"]:
        med = df[col].expanding(min_periods=20).median().shift(1)
        df[col] = df[col] - med
    return df


def make_legs_fn(day, direction, kind):
    def f(spot):
        K = atm_strike(spot)
        if kind == "credit":
            S = day.px(K, "CE", rp.ENTRY_MIN) + day.px(K, "PE", rp.ENTRY_MIN)
            d = max(50, int(round(0.5 * S / 50)) * 50)
            if direction > 0:
                return [("PE", K - d, -1), ("PE", K - d - SPREAD_W, +1)]
            return [("CE", K + d, -1), ("CE", K + d + SPREAD_W, +1)]
        if direction > 0:
            return [("CE", K, +1), ("CE", K + SPREAD_W, -1)]
        return [("PE", K, +1), ("PE", K - SPREAD_W, -1)]
    return f


def run_spreads(days, cyc, directions, kind, stop, tp):
    rows = []
    for window, exp in cyc:
        e = window[0]
        dirn = directions.get(e, 0)
        if not dirn or np.isnan(dirn):
            continue
        r = rp.run_cycle(days, window, make_legs_fn(days[e], int(dirn), kind), stop, tp)
        if r:
            r["dir"] = int(dirn)
            rows.append(r)
    return pd.DataFrame(rows)


def summarize(df, name):
    out = {"strategy": name}
    for label, sub in [("IS", df[df.entry < "2024-01-01"]), ("OOS", df[df.entry >= "2024-01-01"])]:
        s = sub["rs"] if len(sub) else pd.Series(dtype=float)
        eq = s.cumsum()
        out[f"{label}_n"] = len(s)
        out[f"{label}_total"] = round(s.sum())
        out[f"{label}_avg"] = round(s.mean(), 0) if len(s) else np.nan
        out[f"{label}_win%"] = round((s > 0).mean() * 100, 1) if len(s) else np.nan
        out[f"{label}_sr"] = round(s.mean() / s.std() * np.sqrt(52), 2) if len(s) > 2 and s.std() > 0 else np.nan
        out[f"{label}_dd"] = round((eq - eq.cummax()).min()) if len(s) else np.nan
    return out


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    days, daily = load_days(), load_daily()
    cyc = rp.cycles(daily, days)
    sig = signal_table(daily, days, cyc)
    sig.to_csv("data/reports/weekly_signals.csv")
    sigcols = [c for c in sig.columns if c not in ("expiry", "y")]

    # ---- 1. Predictive power: does the signal's SIGN call the week's direction?
    print("=== Weekly direction hit-rate & rank IC (entry 09:30 -> expiry 15:15) ===")
    rows = []
    for col in sigcols:
        for label, sub in [("IS", sig[sig.index < "2024-01-01"]), ("OOS", sig[sig.index >= "2024-01-01"])]:
            x = sub[[col, "y"]].dropna()
            x = x[x[col] != 0]
            if len(x) < 25:
                continue
            hit = (np.sign(x[col]) == np.sign(x["y"])).mean() * 100
            rows.append({"signal": col, "set": label, "n": len(x), "hit%": round(hit, 1),
                         "IC": round(spearmanr(x[col], x["y"]).correlation, 3)})
    ic = pd.DataFrame(rows).pivot(index="signal", columns="set", values=["n", "hit%", "IC"])
    print(ic.to_string())
    base = sig["y"]
    print("\nBase rate: weeks up = IS %.1f%%, OOS %.1f%%" % ((base[base.index < "2024-01-01"] > 0).mean() * 100,
                                                          (base[base.index >= "2024-01-01"] > 0).mean() * 100))

    # ---- 2. Trade every signal with spreads; compare to always-bull / always-bear baselines
    rp.QTY = 75
    results = []
    variants = [("credit", None, None), ("credit", 1.5, None), ("debit", None, None), ("debit", None, 1.0)]
    for kind, stop, tp in variants:
        tag = f"{kind}" + (f"_sl{stop}" if stop else "") + (f"_tp{tp}" if tp else "")
        for name, dirs in [("ALWAYS_BULL", {e: 1 for e in sig.index}), ("ALWAYS_BEAR", {e: -1 for e in sig.index})] + \
                          [(col, np.sign(sig[col]).to_dict()) for col in sigcols]:
            df = run_spreads(days, cyc, dirs, kind, stop, tp)
            if df.empty:
                continue
            results.append({**summarize(df, name), "variant": tag})
        print("done", tag)
    res = pd.DataFrame(results)
    res.to_csv("data/reports/research_directional_spreads.csv", index=False)
    for tag, g in res.groupby("variant"):
        print(f"\n=== {tag} ===")
        print(g.drop(columns="variant").sort_values("IS_sr", ascending=False).to_string(index=False))
