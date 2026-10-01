"""
Contract-exact options backtest engine on DhanHQ 1-minute data.

Market data is stored per trading day as dense [minute x strike] matrices (minute 0 = 09:15,
375 = 15:30) for CE/PE close and high, forward-filled in time (last traded price). Positions
are always keyed by ABSOLUTE strike, so a leg is the same contract from entry to exit.

If a strike drifts outside the +/-10-strike window Dhan stores, its price is reconstructed as
intrinsic value (conservative for shorts that are deep ITM; ~0 for far OTM legs).
"""
from pathlib import Path
import pickle
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data" / "processed"
CACHE = DATA / "engine_cache.pkl"
NMIN = 376
WINDOW_PTS = 500  # Dhan stores ATM-10..ATM+10 strikes (50-pt steps)


class Day:
    __slots__ = ("date", "strikes", "sidx", "ce", "pe", "ce_hi", "pe_hi", "spot", "ce_iv", "pe_iv")

    def col(self, strike):
        return self.sidx.get(float(strike))

    def px(self, strike, typ, m, high=False):
        """Option price at minute m for an absolute strike; intrinsic fallback."""
        j = self.col(strike)
        if j is not None:
            arr = (self.ce_hi if high else self.ce) if typ == "CE" else (self.pe_hi if high else self.pe)
            v = arr[m, j]
            if not np.isnan(v):
                return float(v)
        s = self.spot[m]
        return max(0.05, (s - strike) if typ == "CE" else (strike - s))

    def series(self, strike, typ, m0=0, m1=NMIN - 1, high=False):
        j = self.col(strike)
        s = self.spot[m0:m1 + 1]
        intrinsic = np.maximum(0.05, (s - strike) if typ == "CE" else (strike - s))
        if j is None:
            return intrinsic
        arr = (self.ce_hi if high else self.ce) if typ == "CE" else (self.pe_hi if high else self.pe)
        v = arr[m0:m1 + 1, j].astype(np.float64)
        return np.where(np.isnan(v), intrinsic, v)


def _build():
    opt = pd.read_parquet(DATA / "options_1min.parquet")
    days = {}
    for date, g in opt.groupby("date", sort=True):
        strikes = np.sort(g["strike"].unique())
        sidx = {float(k): i for i, k in enumerate(strikes)}
        mi = g["minute"].to_numpy()
        si = np.array([sidx[float(k)] for k in g["strike"].to_numpy()])
        d = Day()
        d.date = date
        d.strikes = strikes
        d.sidx = sidx
        sp = np.full(NMIN, np.nan)
        spg = g.groupby("minute")["spot"].last()
        sp[spg.index.to_numpy()] = spg.to_numpy()
        d.spot = pd.Series(sp).ffill().bfill().to_numpy()
        # A strike that has drifted outside Dhan's +/-500 window has NO price. Forward-filling it
        # would freeze a stale price (e.g. a deep-ITM short put stuck 150 pts below intrinsic).
        # Only fill short data gaps while the strike is still inside the window; outside -> NaN
        # so Day.px/series fall back to intrinsic value.
        atm = np.round(d.spot / 50.0) * 50.0
        outside = np.abs(strikes[None, :] - atm[:, None]) > WINDOW_PTS
        for name, colname in [("ce", "ce_close"), ("pe", "pe_close"), ("ce_hi", "ce_high"),
                              ("pe_hi", "pe_high"), ("ce_iv", "ce_iv"), ("pe_iv", "pe_iv")]:
            a = np.full((NMIN, len(strikes)), np.nan, dtype=np.float32)
            a[mi, si] = g[colname].to_numpy()
            a = pd.DataFrame(a).ffill(limit=5).to_numpy(dtype=np.float32, copy=True)
            a[outside] = np.nan
            setattr(d, name, a)
        days[date] = d
    return days


def load_days(rebuild=False):
    if CACHE.exists() and not rebuild:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    days = _build()
    with open(CACHE, "wb") as f:
        pickle.dump(days, f, protocol=pickle.HIGHEST_PROTOCOL)
    return days


def load_daily():
    d = pd.read_parquet(DATA / "daily_features.parquet")
    d.index = pd.to_datetime(d.index)
    return d


def load_spot_minutes():
    """Spot 1-min closes per day as arrays (minute-indexed)."""
    p = Path(__file__).resolve().parent / "data" / "raw" / "minute_parquet" / "nifty_spot_1min.parquet"
    s = pd.read_parquet(p, columns=["date", "time", "open", "high", "low", "close"])
    hh = s["time"].str.slice(0, 2).astype(int)
    mm = s["time"].str.slice(3, 5).astype(int)
    s["minute"] = hh * 60 + mm - 555
    s = s[(s["minute"] >= 0) & (s["minute"] < NMIN)]
    out = {}
    for date, g in s.groupby("date"):
        arr = np.full((NMIN, 4), np.nan)
        arr[g["minute"].to_numpy()] = g[["open", "high", "low", "close"]].to_numpy()
        out[date] = pd.DataFrame(arr).ffill().to_numpy()
    return out


def atm_strike(spot, step=50):
    return round(spot / step) * step


if __name__ == "__main__":
    import time
    t = time.time()
    days = load_days(rebuild=True)
    print(len(days), "days built in", round(time.time() - t, 1), "s")
    d = days["2024-03-07"]
    k = atm_strike(d.spot[5])
    print("2024-03-07 ATM", k, "CE 09:20", d.px(k, "CE", 5), "CE 15:20", d.px(k, "CE", 365))
