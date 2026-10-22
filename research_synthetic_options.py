"""
Synthetic weekly Nifty option prices for the sessions with no option data (2019-11-18 .. 2020-12-31),
calibrated on the real Dhan prices, then Strategy 6 replayed on them to see its worst phase (COVID 2020).

Pricing model (all fitted on 2021-2023 real prices; 2024+ is the check):
  * Black-76 on F = S * exp(r T), T = (sessions left after today + fraction of today left) / 252,
    expiry 15:30 of the expiry session. r is fitted from ATM put-call parity.
  * Volatility input = India VIX, modelled per minute because pre-2021 VIX is daily only:
        ln v_m = ln v_prev + beta * ln(S_m / S_prev) + (m/375) * (ln v_close - ln v_prev - beta * ln(S_375 / S_prev))
    (moves with spot intraday, ends at the real VIX close). beta fitted on 2021-08+ minute VIX.
    The day's VIX close only shapes the simulated market prices; strategies still decide on the
    previous session's VIX close.
  * Correction surface: IV / v as a function of sessions to expiry (0..4), time of day (3 buckets),
    VIX level (5 buckets) and standardised moneyness z = ln(K/F) / (v sqrt(T)). Median of the real
    implied vols per cell, linearly interpolated in z. This adds the skew, the weekly-vs-30-day
    level and the intraday 0DTE decay that plain BS(VIX) misses.
  * 1-minute highs: CE priced at the spot high, PE at the spot low.

Expiries before 2021: Thursday, moved to the previous session when Thursday is a holiday (checked
against the real expiries 2021-01..2025-08).

Usage:
  python research_synthetic_options.py calibrate   # fits beta, r, surface -> data/reports/synthetic_iv_surface.json
  python research_synthetic_options.py check       # synthetic vs real prices + Strategy 6 replay 2021-26 synthetic vs real
  python research_synthetic_options.py stress      # Strategy 6 on 2019-11..2020-12 synthetic (+ stress cases), and the
                                                   # chained history: synthetic 2019-20 then real 2021+
Writes data/reports/synthetic_*.csv / .json
"""
import json
import sys

import numpy as np
import pandas as pd
from scipy.special import ndtr

import cost_model
import engine
import research_realtrade as rt
from research_realtrade_portfolio import sleeves

REP = "data/reports/"
SURF = REP + "synthetic_iv_surface.json"
CAL_END = "2023-12-31"
ZC = np.arange(-4.0, 4.0001, 0.25)
VIX_EDGES = [12, 14, 17, 21]
TOD_EDGES = [30, 75, 135, 195, 255, 315]   # 09:45, 10:30, 11:30, 12:30, 13:30, 14:30
NT = len(TOD_EDGES) + 1
GRID_PTS = 2500                   # synthetic strikes: day's opening ATM +/- 2500
NEAR_T = 1.0 / 375 / 252


# ------------------------------------------------------------------ inputs
_C = {}


def spot_minutes():
    if "spot" not in _C:
        _C["spot"] = engine.load_spot_minutes()
    return _C["spot"]


def vix_daily():
    if "vix" not in _C:
        _C["vix"] = pd.read_csv("data/raw/india_vix_daily_dhan.csv").set_index("date")["close"]
    return _C["vix"]


def trading_dates_pre2021():
    """Real sessions before 2021: weekdays in the Dhan spot data plus the 2020-02-01 Budget Saturday."""
    out = []
    for d in sorted(spot_minutes()):
        if d >= "2021-01-01":
            break
        if pd.Timestamp(d).dayofweek < 5 or d == "2020-02-01":
            out.append(d)
    return out


def rule_expiries(dates):
    """Weekly expiry = Thursday, or the last session before it in the same week if Thursday is a holiday."""
    s = pd.Series(pd.to_datetime(dates))
    wk = s.dt.to_period("W-SUN")
    out = set()
    for _, g in s.groupby(wk):
        thu = g.iloc[0] - pd.Timedelta(days=g.iloc[0].dayofweek) + pd.Timedelta(days=3)
        c = g[g <= thu]
        if len(c):
            out.add(c.max().strftime("%Y-%m-%d"))
    return out


def daily_frame():
    """engine.load_daily() with rule expiries before 2021 and the previous-session VIX close (as rt.data())."""
    daily = engine.load_daily().copy()
    pre = trading_dates_pre2021()
    exp = rule_expiries(pre)
    idx = daily.index.strftime("%Y-%m-%d")
    daily = daily[(idx >= "2021-01-01") | idx.isin(pre)]
    idx = daily.index.strftime("%Y-%m-%d")
    pre_mask = idx < "2021-01-01"
    daily["is_expiry"] = daily["is_expiry"].astype(object)
    daily.loc[pre_mask, "is_expiry"] = [d in exp for d in idx[pre_mask]]
    v = pd.read_csv("data/raw/india_vix_daily_dhan.csv", parse_dates=["date"]).set_index("date")["close"]
    daily["vix_prev"] = v.reindex(v.index.union(daily.index)).ffill().shift(1).reindex(daily.index)
    return daily


# ------------------------------------------------------------------ model
def vix_path(date, prev_date, S, beta):
    """Modelled VIX per minute (len(S)) for `date`; S = spot closes per minute."""
    V = vix_daily()
    vp = V.loc[:prev_date].iloc[-1]
    vc = V.get(date, vp)
    sp = spot_minutes()[prev_date][:, 3]
    sp = sp[~np.isnan(sp)][-1]
    r = np.log(S / sp)
    m = np.arange(len(S))
    return np.exp(np.log(vp) + beta * r + (m / 375.0) * (np.log(vc) - np.log(vp) - beta * r[-1]))


def t_years(dte, m):
    return np.maximum((dte + (375 - np.asarray(m)) / 375.0) / 252.0, NEAR_T)


def black(typ_ce, K, F, T, sig, r):
    sq = sig * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    df = np.exp(-r * T)
    c = df * (F * ndtr(d1) - K * ndtr(d2))
    p = df * (K * ndtr(-d2) - F * ndtr(-d1))
    return np.where(typ_ce, c, p)


def bins(dte, m, v):
    return (np.minimum(np.asarray(dte), 4), np.searchsorted(TOD_EDGES, m, side="right"),
            np.searchsorted(VIX_EDGES, v, side="right"))


class Model:
    def __init__(self, path=SURF):
        with open(path) as f:
            js = json.load(f)
        self.beta, self.r = js["beta"], js["r"]
        self.R = np.array(js["ratio"])          # [dte, tod, vixb, z]
        self.hi_mult = 1.0                      # stress: scale IV when VIX > 21 (beyond most calibration data)
        self.trend = False                      # True: extrapolate the real 2021-26 rise of weekly IV/VIX with VIX

    def ratio(self, dte, m, v, z):
        d, t, b = bins(dte, m, v)
        d, t, b = np.broadcast_arrays(d, t, b)
        z = np.broadcast_to(z, d.shape)
        out = np.empty(d.shape)
        key = d * 100 + t * 10 + b
        for k in np.unique(key):
            sel = key == k
            out[sel] = np.interp(z[sel], ZC, self.R[k // 100, (k // 10) % 10, k % 10])
        return out

    def price(self, typ_ce, K, S, dte, m, v):
        T = t_years(dte, m)
        F = S * np.exp(self.r * T)
        vv = np.asarray(v) / 100.0
        z = np.log(K / F) / (vv * np.sqrt(T))
        sig = vv * self.ratio(dte, m, v, z) * np.where(np.asarray(v) > VIX_EDGES[-1], self.hi_mult, 1.0)
        if self.trend:
            # ATM weekly IV/VIX on real data: 0.89 at VIX 21-24, 0.93 at 24-27, 1.06 at 27-35 ~ VIX^0.15
            sig = sig * (np.maximum(np.asarray(v), 22.5) / 22.5) ** 0.15
        p = black(typ_ce, K, F, T, sig, self.r)
        return np.maximum(0.05, np.round(p / 0.05) * 0.05)


# ------------------------------------------------------------------ synthetic days (engine.Day interface)
class _Lazy:
    """[minute, strike-column] price matrix computed one column at a time on first access."""

    def __init__(self, day, ce, spot):
        self.day, self.ce, self.spot, self.cols = day, ce, spot, {}

    def __getitem__(self, key):
        m, j = key
        col = self.cols.get(j)
        if col is None:
            d = self.day
            col = d.model.price(self.ce, d.strikes[j], self.spot, d.dte, d.mins, d.v)
            self.cols[j] = col
        return col[m]


class SynthDay:
    def __init__(self, date, prev_date, dte, model):
        a = spot_minutes()[date]
        a = pd.DataFrame(a).bfill().to_numpy()
        self.date, self.dte, self.model = date, dte, model
        self.spot = a[:, 3]
        self.mins = np.arange(engine.NMIN)
        self.v = vix_path(date, prev_date, self.spot, model.beta)
        k0 = engine.atm_strike(self.spot[0])
        self.strikes = np.arange(k0 - GRID_PTS, k0 + GRID_PTS + 1, 50.0)
        self.sidx = {float(k): i for i, k in enumerate(self.strikes)}
        self.ce, self.pe = _Lazy(self, True, self.spot), _Lazy(self, False, self.spot)
        self.ce_hi, self.pe_hi = _Lazy(self, True, a[:, 1]), _Lazy(self, False, a[:, 2])


def synth_days(dates, daily, model):
    """SynthDay per date (needs the previous session for VIX/spot anchors and the expiry calendar)."""
    idx = list(daily.index.strftime("%Y-%m-%d"))
    pos = {d: i for i, d in enumerate(idx)}
    exp = set(daily.index[daily["is_expiry"] == True].strftime("%Y-%m-%d"))
    out = {}
    for d in dates:
        i = pos.get(d)
        if not i:
            continue
        dte = 0
        for x in idx[i:]:
            if x in exp:
                break
            dte += 1
        else:
            continue
        out[d] = SynthDay(d, idx[i - 1], dte, model)
    return out


# ------------------------------------------------------------------ calibration
def implied_vol(typ_ce, p, K, F, T, r, lo=0.01, hi=4.0, it=45):
    lo, hi = np.full(p.shape, lo), np.full(p.shape, hi)
    for _ in range(it):
        mid = 0.5 * (lo + hi)
        up = black(typ_ce, K, F, T, mid, r) > p
        hi = np.where(up, mid, hi)
        lo = np.where(up, lo, mid)
    return 0.5 * (lo + hi)


def real_sample(d0, d1, step):
    """Real option rows (every `step` minutes, 09:20-15:10) with session context: dte, prev date."""
    o = pd.read_parquet(engine.DATA / "options_1min.parquet",
                        columns=["date", "minute", "strike", "ce_close", "pe_close", "spot"],
                        filters=[("date", ">=", d0), ("date", "<=", d1)])
    o = o[(o.minute % step == 0) & (o.minute >= 5) & (o.minute <= 355)]
    daily = daily_frame()
    days = {d: None for d in o.date.unique()}
    info, tds = rt.calendar(days, daily)
    idx = list(daily.index.strftime("%Y-%m-%d"))
    prev = {d: idx[i - 1] for i, d in enumerate(idx) if i}
    o = o[o.date.isin(info.keys()) & o.date.isin(prev.keys())]
    o["dte"] = o.date.map(lambda d: info[d][1] - 1 - info[d][0])
    return o, prev


def add_vix(o, prev, beta):
    v = np.empty(len(o))
    sp = spot_minutes()
    for d, g in o.groupby("date"):
        S = pd.DataFrame(sp[d]).bfill().to_numpy()[:, 3]
        v[o.index.get_indexer(g.index)] = vix_path(d, prev[d], S, beta)[g.minute.to_numpy()]
    o["v"] = v
    return o


def fit_beta():
    vm = pd.read_parquet("data/raw/minute_parquet/india_vix_1min.parquet", columns=["date", "time", "close"])
    vm["minute"] = vm.time.str.slice(0, 2).astype(int) * 60 + vm.time.str.slice(3, 5).astype(int) - 555
    vm = vm[(vm.minute >= 0) & (vm.minute <= 375)]
    V = vix_daily()
    sp = spot_minutes()
    dates = sorted(vm.date.unique())
    xs, ys, rows = [], [], []
    for d, g in vm.groupby("date"):
        if d not in sp or d not in V.index:
            continue
        pv = V.loc[:d].iloc[:-1]
        sdates = [x for x in sp if x < d]
        if not len(pv) or not sdates:
            continue
        S = pd.DataFrame(sp[d]).bfill().to_numpy()[:, 3]
        Sp = sp[max(sdates)][:, 3]
        Sp = Sp[~np.isnan(Sp)][-1]
        m = g.minute.to_numpy()
        xs.append(np.log(S[m] / Sp))
        ys.append(np.log(g.close.to_numpy() / pv.iloc[-1]))
        rows.append((d, m, g.close.to_numpy()))
    x, y = np.concatenate(xs), np.concatenate(ys)
    ok = np.isfinite(x) & np.isfinite(y)
    beta = float((x[ok] * y[ok]).sum() / (x[ok] ** 2).sum())
    return beta, rows


def calibrate():
    beta, rows = fit_beta()
    # how well the bridge reproduces the real minute VIX
    errs = []
    for d, m, vreal in rows[::5]:
        idx = sorted(x for x in spot_minutes() if x < d)
        S = pd.DataFrame(spot_minutes()[d]).bfill().to_numpy()[:, 3]
        errs.append(np.abs(np.log(vix_path(d, idx[-1], S, beta)[m] / vreal)))
    print(f"beta (dlnVIX / dlnSpot, intraday) = {beta:.2f}; bridge |log error| median "
          f"{100 * np.median(np.concatenate(errs)):.1f}%  p90 {100 * np.quantile(np.concatenate(errs), .9):.1f}%")

    o, prev = real_sample("2021-01-01", CAL_END, 5)
    o = add_vix(o, prev, beta)
    m, S, K, dte = o.minute.to_numpy(), o.spot.to_numpy(), o.strike.to_numpy(), o.dte.to_numpy()
    T = t_years(dte, m)
    # carry from ATM parity (dte >= 1)
    atm = (np.abs(K - S) <= 25) & (dte >= 1) & o.ce_close.notna().to_numpy() & o.pe_close.notna().to_numpy()
    Fi = K[atm] + o.ce_close.to_numpy()[atm] - o.pe_close.to_numpy()[atm]
    r = float(np.median(np.log(Fi / S[atm]) / T[atm]))
    print(f"carry r (ATM parity, median) = {100 * r:.2f}%/yr")
    F = S * np.exp(r * T)
    ce = K >= F
    p = np.where(ce, o.ce_close.to_numpy(), o.pe_close.to_numpy())
    intr = np.where(ce, np.maximum(F - K, 0), np.maximum(K - F, 0))
    ok = np.isfinite(p) & (p >= 0.5) & (p > intr + 0.1) & (np.abs(K - S) <= 500)
    vv = o.v.to_numpy() / 100
    iv = implied_vol(ce[ok], p[ok], K[ok], F[ok], T[ok], r)
    z = np.log(K[ok] / F[ok]) / (vv[ok] * np.sqrt(T[ok]))
    ratio = iv / vv[ok]
    good = (iv > 0.011) & (iv < 3.9)
    d_, t_, b_ = bins(dte[ok], m[ok], o.v.to_numpy()[ok])
    zi = np.clip(np.round((z + 4) / 0.25).astype(int), 0, len(ZC) - 1)
    df = pd.DataFrame(dict(d=d_, t=t_, b=b_, z=zi, ratio=ratio))[good]
    g = df.groupby(["d", "t", "b", "z"]).ratio.agg(["median", "size"]).reset_index()
    R = np.full((5, NT, 5, len(ZC)), np.nan)
    N = np.zeros((5, NT, 5, len(ZC)), int)
    for row in g.itertuples():
        N[row.d, row.t, row.b, row.z] = row[6]
        if row[6] >= 20:
            R[row.d, row.t, row.b, row.z] = row[5]
    # fill in z, then empty rows from the nearest VIX bucket, then nearest time-of-day bucket
    for d in range(5):
        for t in range(NT):
            for b in range(5):
                v = R[d, t, b]
                if np.isfinite(v).any():
                    f = np.isfinite(v)
                    R[d, t, b] = np.interp(ZC, ZC[f], v[f])
    for d in range(5):
        for t in range(NT):
            for b in range(5):
                if np.isnan(R[d, t, b]).all():
                    for bb in sorted(range(5), key=lambda x: abs(x - b)):
                        if np.isfinite(R[d, t, bb]).all():
                            R[d, t, b] = R[d, t, bb]
                            break
            for b in range(5):
                if np.isnan(R[d, t, b]).all():
                    for tt in sorted(range(NT), key=lambda x: abs(x - t)):
                        if np.isfinite(R[d, tt, b]).all():
                            R[d, t, b] = R[d, tt, b]
                            break
    assert np.isfinite(R).all()
    with open(SURF, "w") as f:
        json.dump(dict(beta=beta, r=r, fit=f"2021-01-01..{CAL_END}", z=ZC.tolist(), vix_edges=VIX_EDGES,
                       tod_edges=TOD_EDGES, ratio=R.round(4).tolist(), n=N.tolist()), f)
    print("ATM (z=0) IV/VIX by sessions-to-expiry x VIX bucket, mid-day:")
    print(pd.DataFrame(R[:, 3, :, 16].round(2), index=[f"dte{d}" for d in range(5)],
                       columns=["<12", "12-14", "14-17", "17-21", "21+"]).to_string())
    print("skew at dte2 mid-day VIX 14-17, z=-3..3:", R[2, 1, 2, 4:29:4].round(2).tolist())
    print("wrote", SURF)


# ------------------------------------------------------------------ checks
def price_check():
    """Synthetic vs real prices (every 15 min, |K-S| <= 450) in-sample 2021-23 and out-of-sample 2024+."""
    mdl = Model()
    rows = []
    for name, d0, d1 in [("IS 2021-23", "2021-01-01", CAL_END), ("OOS 2024+", "2024-01-01", "2099")]:
        o, prev = real_sample(d0, d1, 15)
        o = add_vix(o, prev, mdl.beta)
        for typ in ("CE", "PE"):
            p = o[f"{typ.lower()}_close"].to_numpy()
            K, S, m, dte, v = (o[c].to_numpy() for c in ("strike", "spot", "minute", "dte", "v"))
            sp = mdl.price(typ == "CE", K, S, dte, m, v)
            otm = (K - S) if typ == "CE" else (S - K)
            mb = np.select([np.abs(otm) <= 50, otm > 250, otm > 50, otm < -50], ["ATM", "OTM 250-450", "OTM 50-250", "ITM"], "near ITM")
            df = pd.DataFrame(dict(dte=np.minimum(dte, 4), mb=mb, real=p, syn=sp))
            df = df[np.isfinite(df.real) & (df.real >= 1) & (np.abs(otm) <= 450)]
            for (d, b), g in df.groupby(["dte", "mb"]):
                rows.append(dict(period=name, typ=typ, dte=d, moneyness=b, n=len(g),
                                 median_syn_over_real=round(float(np.median(g.syn / g.real)), 3),
                                 median_abs_err_pct=round(100 * float(np.median(np.abs(g.syn / g.real - 1))), 1),
                                 sum_syn_over_sum_real=round(float(g.syn.sum() / g.real.sum()), 3)))
    R = pd.DataFrame(rows)
    R.to_csv(REP + "synthetic_price_check.csv", index=False)
    pv = R[R.moneyness.isin(["ATM", "OTM 50-250", "OTM 250-450"])].pivot_table(
        index=["period", "dte"], columns=["typ", "moneyness"], values="median_syn_over_real")
    print("median synthetic / real price (1.00 = unbiased):")
    print(pv.round(2).to_string())
    pe = R[R.moneyness.isin(["ATM", "OTM 50-250", "OTM 250-450"])].groupby("period").median_abs_err_pct.median()
    print("median abs error %:", pe.to_dict())


def run(days, daily, d0, d1, start=1_000_000, slip_mult=1.0, margin_pct=0.09, w=(0.45, 0.45, 0.05)):
    base, saved = cost_model.slip, dict(rt._DATA)
    rt.MARGIN_PCT = margin_pct
    rt.slip = lambda p, urgent=False: slip_mult * base(p, urgent)
    rt._DATA.clear()
    rt._DATA.update(days=days, daily=daily)
    try:
        E, T, acct = rt.replay(sleeves(*w), start, True, d0, d1)
    finally:
        rt.slip, rt.MARGIN_PCT = base, 0.09
        rt._DATA.clear()
        rt._DATA.update(saved)
    return E, T, acct


def summary(name, E, T, acct, start=1_000_000):
    m = rt.metrics(E, T, start)
    tr = T[T.sleeve != "running"]
    e = np.concatenate([[start], E.equity.to_numpy()])
    peak = np.maximum.accumulate(e)
    i = int(np.argmin(e / peak))
    j = int(np.argmax(e[:i + 1]))
    dates = [E.date.iloc[0]] + list(E.date)
    rec = next((dates[k] for k in range(i, len(e)) if e[k] >= peak[i]), "not recovered")
    return dict(name=name, **m, dd_peak=dates[j], dd_trough=dates[i], dd_recovered=rec,
                years=rt.by_year(E, start), unobserved=acct.unobserved,
                pf_by_sleeve={s: rt.pf(g.pnl) for s, g in tr.groupby("sleeve")},
                n_by_sleeve=tr.groupby("sleeve").size().to_dict())


def show(s):
    print(f"{s['name']:34s} CAGR {s['cagr']:6.1f} DD {s['max_dd']:6.1f} ({s['dd_peak']} -> {s['dd_trough']}, "
          f"rec {s['dd_recovered']}) PF {s['pf_trades']:.2f} n {s['trades']} win {s['win']} "
          f"| {s['pf_by_sleeve']} {s['n_by_sleeve']} | {s['years']}", flush=True)


def strategy_check():
    """Strategy 6 on 2021-26: real replay vs the same rules on synthetic prices of the same sessions."""
    mdl = Model()
    daily = daily_frame()
    real_days = engine.load_days()
    syn = synth_days(sorted(real_days), daily, mdl)
    out = {}
    res = {}
    for name, days in [("real 2021-26", real_days), ("synthetic 2021-26", syn)]:
        E, T, acct = run(days, daily, "2021-01-01", "2099")
        s = summary(name, E, T, acct)
        show(s)
        out[name] = s
        res[name] = T
        T.to_csv(REP + f"synthetic_check_{name.split()[0]}_trades.csv", index=False)
    a, b = res["real 2021-26"], res["synthetic 2021-26"]
    a, b = a[a.sleeve != "running"], b[b.sleeve != "running"]
    j = a.merge(b, on=["sleeve", "entry"], suffixes=("_real", "_syn"))
    for sl, g in j.groupby("sleeve"):
        # per-trade comparison in return terms (lots differ with equity)
        x = g.pnl_real / g.lots_real
        y = g.pnl_syn / g.lots_syn
        print(f"  {sl}: {len(g)} matched trades (real {len(a[a.sleeve == sl])}, syn {len(b[b.sleeve == sl])}); "
              f"P&L per lot corr {np.corrcoef(x, y)[0, 1]:.2f}; mean per lot real {x.mean():.0f} syn {y.mean():.0f}; "
              f"credit per lot real {(g.credit_real / g.lots_real).mean():.0f} syn {(g.credit_syn / g.lots_syn).mean():.0f}; "
              f"stops real {(g.reason_real == 'stop').mean():.0%} syn {(g.reason_syn == 'stop').mean():.0%}")
        out[f"match {sl}"] = dict(n=len(g), corr=round(float(np.corrcoef(x, y)[0, 1]), 2),
                                  mean_real=round(float(x.mean())), mean_syn=round(float(y.mean())))
    with open(REP + "synthetic_strategy_check.json", "w") as f:
        json.dump(out, f, indent=1, default=str)


def expiry_check():
    daily = engine.load_daily()
    idx = daily.index.strftime("%Y-%m-%d")
    sel = (idx >= "2021-01-01") & (idx <= "2025-08-31") & (pd.Series(daily.index.dayofweek < 5, index=daily.index).to_numpy())
    dates = list(idx[sel])
    real = set(idx[sel & (daily.is_expiry == True).to_numpy()])
    rule = rule_expiries(dates)
    print(f"expiry rule vs real 2021-01..2025-08: {len(real & rule)} match, real-only {sorted(real - rule)}, "
          f"rule-only {sorted(rule - real)}")


def stress():
    mdl = Model()
    daily = daily_frame()
    pre = trading_dates_pre2021()
    syn = synth_days(pre, daily, mdl)
    print("2019-20 VIX:", vix_daily().loc["2019-11-18":"2020-12-31"].describe().round(1).to_dict())
    print(f"synthetic sessions {min(syn)}..{max(syn)}: {len(syn)}; expiries "
          f"{int(daily.loc[:'2020-12-31', 'is_expiry'].sum())}")
    out = {}
    cases = [("base 10L", {}), ("slippage x2", dict(slip_mult=2.0)), ("slippage x3", dict(slip_mult=3.0)),
             ("margin 12%", dict(margin_pct=0.12)), ("margin 15%", dict(margin_pct=0.15)),
             ("start 5L", dict(start=500_000)), ("half size", dict(w=(0.225, 0.225, 0.025)))]
    for name, kw in cases:
        E, T, acct = run(syn, daily, "2019-11-18", "2020-12-31", **kw)
        s = summary("2019-20 syn " + name, E, T, acct, kw.get("start", 1_000_000))
        show(s)
        out[s["name"]] = s
        if name == "base 10L":
            T.to_csv(REP + "synthetic_2019_20_trades.csv", index=False)
            E.to_csv(REP + "synthetic_2019_20_equity.csv", index=False)
            Em = E.assign(month=E.date.str[:7]).groupby("month").equity.last()
            mret = (Em / Em.shift(1).fillna(1_000_000) - 1) * 100
            print("monthly returns %:", mret.round(1).to_dict())
            tr = T[T.sleeve != "running"]
            print("worst 10 trades:")
            print(tr.nsmallest(10, "pnl")[["sleeve", "entry", "exit", "reason", "pnl", "credit", "lots", "vix"]].to_string(index=False))
            ph = tr[(tr.entry >= "2020-02-15") & (tr.entry <= "2020-04-30")]
            print("Feb 15 - Apr 30 2020 trades:")
            print(ph[["sleeve", "entry", "exit", "reason", "pnl", "credit", "lots", "vix"]].to_string(index=False))
    for name, mult, trend, kw in [("IV x0.85 when VIX>21", 0.85, False, {}), ("IV x1.2 when VIX>21", 1.2, False, {}),
                                  ("IV trend above VIX 21", 1.0, True, {}),
                                  ("IV trend + slippage x3", 1.0, True, dict(slip_mult=3.0)),
                                  ("slippage x3 + margin 15%", 1.0, False, dict(slip_mult=3.0, margin_pct=0.15)),
                                  ("IV x0.85 + slippage x3 + margin 15%", 0.85, False,
                                   dict(slip_mult=3.0, margin_pct=0.15))]:
        m2 = Model()
        m2.hi_mult, m2.trend = mult, trend
        E, T, acct = run(synth_days(pre, daily, m2), daily, "2019-11-18", "2020-12-31", **kw)
        s = summary("2019-20 syn " + name, E, T, acct)
        show(s)
        out[s["name"]] = s
    # chained: synthetic 2019-20, real 2021+
    days = dict(syn)
    days.update(engine.load_days())
    E, T, acct = run(days, daily, "2019-11-18", "2099")
    s = summary("chained syn 2019-20 + real 2021+", E, T, acct)
    show(s)
    out[s["name"]] = s
    E.to_csv(REP + "synthetic_chained_equity.csv", index=False)
    with open(REP + "synthetic_stress_summary.json", "w") as f:
        json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("calibrate", "all"):
        calibrate()
    if what in ("check", "all"):
        expiry_check()
        price_check()
        strategy_check()
    if what in ("stress", "all"):
        stress()
