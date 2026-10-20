"""
Build clean research datasets from the DhanHQ 1-minute parquet store.

IMPORTANT data fact (verified): options parquet `strike_offset` is ROLLING ATM-relative â€”
the `strike` column changes whenever spot crosses a 50-pt boundary. Any backtest that
follows "ATM+4" through the week silently switches contracts. Everything downstream
therefore keys option prices by ABSOLUTE strike.

The options series is always the NEAREST weekly expiry; it rolls to the next contract
on the session after expiry. Expiry days are detected empirically (the ATM straddle
collapses on expiry day and resets the next morning), which automatically handles
holidays and the Thursday -> Tuesday expiry switch (Sep 2025).

Outputs (data/processed/):
  options_1min.parquet   date, minute(int, 0=09:15), strike, CE/PE close/high/low, iv, spot
  daily_features.parquet one row per trading day: spot OHLC, VIX, expiry flags, FII/DII (lagged)
"""
from pathlib import Path
import glob
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent / "data" / "raw" / "minute_parquet"
OUT = Path(__file__).parent / "data" / "processed"
OUT.mkdir(parents=True, exist_ok=True)


def minute_of_day(time_str: pd.Series) -> pd.Series:
    hh = time_str.str.slice(0, 2).astype(np.int16)
    mm = time_str.str.slice(3, 5).astype(np.int16)
    return (hh * 60 + mm - (9 * 60 + 15)).astype(np.int16)


def build_options():
    files = sorted(glob.glob(str(BASE / "options" / "year=*" / "month=*" / "options.parquet")))
    parts = []
    for f in files:
        d = pd.read_parquet(f, columns=["date", "time", "strike", "option_type", "open", "high",
                                        "low", "close", "iv", "oi", "spot"])
        d["minute"] = minute_of_day(d["time"])
        d = d[(d["minute"] >= 0) & (d["minute"] <= 375)]
        # Wide format: one row per (date, minute, strike), CE and PE side by side
        w = d.pivot_table(index=["date", "minute", "strike"], columns="option_type",
                          values=["open", "high", "low", "close", "iv", "oi"], aggfunc="last")
        w.columns = [f"{t.lower()}_{v}" for v, t in w.columns]
        w = w.reset_index()
        sp = d.groupby(["date", "minute"])["spot"].last().rename("spot").reset_index()
        w = w.merge(sp, on=["date", "minute"], how="left")
        for c in w.columns:
            if w[c].dtype == np.float64:
                w[c] = w[c].astype(np.float32)
        parts.append(w)
        print(f"{f[-32:]}: {len(w):,} rows")
    opt = pd.concat(parts, ignore_index=True)
    opt = opt.sort_values(["date", "minute", "strike"]).reset_index(drop=True)
    opt.to_parquet(OUT / "options_1min.parquet", index=False)
    print("options rows:", len(opt))
    return opt


def detect_expiries(opt: pd.DataFrame) -> pd.DataFrame:
    """Per-day ATM straddle open/close; expiry = straddle at close tiny and next-day open resets."""
    atm = opt.copy()
    atm["dist"] = (atm["strike"] - atm["spot"]).abs()
    atm = atm.loc[atm.groupby(["date", "minute"])["dist"].idxmin()]
    atm["straddle"] = atm["ce_close"] + atm["pe_close"]
    g = atm.groupby("date")
    s = pd.DataFrame({
        "straddle_open": g.apply(lambda x: x.loc[x["minute"] >= 5, "straddle"].iloc[0] if (x["minute"] >= 5).any() else np.nan),
        "straddle_close": g["straddle"].last(),
    })
    s["next_open"] = s["straddle_open"].shift(-1)
    # Expiry day: straddle decays to a small fraction and jumps back up next session
    # The overnight jump to the next weekly contract is the unambiguous signature
    s["is_expiry"] = (s["next_open"] > 1.8 * s["straddle_close"]) &                      ((s["straddle_close"] < 0.65 * s["straddle_open"]) | (s["next_open"] > 1.3 * s["straddle_open"]))
    s.loc[s.index[-1], "is_expiry"] = False
    # A crash-driven IV jump right after an expiry (e.g. 2025-04-04) is not a new expiry
    last = None
    for d in s.index[s["is_expiry"]]:
        if last is not None and (pd.Timestamp(d) - pd.Timestamp(last)).days <= 3:
            s.loc[d, "is_expiry"] = False
        else:
            last = d
    return s


def build_daily(opt: pd.DataFrame):
    spot = pd.read_parquet(BASE / "nifty_spot_1min.parquet")
    spot["minute"] = minute_of_day(spot["time"])
    spot = spot[(spot["minute"] >= 0) & (spot["minute"] <= 375)]
    g = spot.groupby("date")
    daily = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "close_1515": g.apply(lambda x: x.loc[x["minute"] <= 360, "close"].iloc[-1]),
    })
    vix = pd.read_parquet(BASE / "india_vix_1min.parquet")
    vg = vix.groupby("date")
    daily["vix_open"] = vg["open"].first()
    daily["vix_close"] = vg["close"].last()
    daily["vix_0920"] = vix[vix["time"] <= "09:20"].groupby("date")["close"].last()

    exp = detect_expiries(opt)
    daily = daily.join(exp, how="left")
    daily.index = pd.to_datetime(daily.index)
    daily = daily.sort_index()
    # Dhan spot includes NSE Saturday MOCK sessions (e.g. 2022-04-09, a fake +4.7%). Their closes
    # would become Monday's "previous close" (gap signal) and pollute realised vol. Once options
    # data exists, a weekend row is a real special session only if it has options data.
    opt_dates = pd.to_datetime(pd.Series(opt["date"].unique()))
    mock = (daily.index.weekday >= 5) & (daily.index >= opt_dates.min()) & ~daily.index.isin(opt_dates)
    daily = daily[~mock]

    # Realised vol (close-to-close, 20d, annualised %)
    r = np.log(daily["close"]).diff()
    daily["ret"] = r
    daily["rv20"] = r.rolling(20).std() * np.sqrt(252) * 100
    daily["rv5"] = r.rolling(5).std() * np.sqrt(252) * 100
    daily["gap"] = daily["open"] / daily["close"].shift(1) - 1

    # FII/DII participant OI: published ~EOD on day t -> usable from day t+1 open.
    fii_path = OUT / "participant_oi_features.csv"
    if fii_path.exists():
        f = pd.read_csv(fii_path, parse_dates=["date"]).set_index("date").sort_index()
        f["fii_idx_fut_net"] = f["fii_fut_idx_long"] - f["fii_fut_idx_short"]
        f["fii_idx_fut_chg"] = f["fii_idx_fut_net"].diff()
        f["fii_idx_opt_net"] = (f["fii_opt_idx_ce_long"] - f["fii_opt_idx_ce_short"]) - \
                               (f["fii_opt_idx_pe_long"] - f["fii_opt_idx_pe_short"])
        f["fii_idx_opt_chg"] = f["fii_idx_opt_net"].diff()
        f["client_idx_fut_net"] = f["client_fut_idx_long"] - f["client_fut_idx_short"]
        f["pro_idx_fut_net"] = f["pro_fut_idx_long"] - f["pro_fut_idx_short"]
        f["fii_ls"] = f["fii_fut_idx_long"] / f["fii_fut_idx_short"]
        f["fii_ls_z60"] = (f["fii_ls"] - f["fii_ls"].rolling(60).mean()) / f["fii_ls"].rolling(60).std()
        f["fii_chg_z60"] = f["fii_idx_fut_chg"] / f["fii_idx_fut_chg"].rolling(60).std()
        keep = ["fii_idx_fut_net", "fii_idx_fut_chg", "fii_idx_opt_net", "fii_idx_opt_chg",
                "client_idx_fut_net", "pro_idx_fut_net", "fii_ls", "fii_ls_z60", "fii_chg_z60",
                "dii_fut_idx_net"]
        lagged = f[keep].add_prefix("prev_")
        # align: value known at close of day t attached to next trading day
        daily = daily.join(lagged.shift(0), how="left")
        daily[lagged.columns] = daily[lagged.columns].shift(1)
    daily.to_parquet(OUT / "daily_features.parquet")
    print(daily.tail())
    print("expiries detected:", int(daily["is_expiry"].sum()))
    print(daily[daily["is_expiry"] == True].index.day_name().value_counts())


if __name__ == "__main__":
    import sys
    p = OUT / "options_1min.parquet"
    opt = pd.read_parquet(p) if (p.exists() and "--rebuild" not in sys.argv) else build_options()
    build_daily(opt)
