"""
Monthly short straddle, entered near 45 DTE and closed at 21 DTE, on 60-minute data.

Data: data/raw/hourly_parquet/options_monthly/ (download_monthly_options.py). Dhan serves two
rolling monthly series, current month (expiry_code 1) and next month (code 2). A contract is
followed by absolute strike: it sits in code 2 until the previous monthly expiry, then in code 1.

Pricing of a leg at a bar, in this order (the `px_*` columns say which one the exit used):
  real    the contract's own close at that bar
  parity  the other option type at the same strike is real: C - P = F - K
  model   strike outside the stored window (+/-10 strikes, +/-3 for next-month from 2026):
          Black-76 on the forward implied by put-call parity, IV extrapolated linearly from the
          OTM strikes nearest the missing one. Model-priced fills pay urgent slippage.
Never forward-filled.

Monthly expiry = last `daily_features.is_expiry` session of each calendar month; months beyond
daily_features use the last such weekday of the month.

Bars are stamped at their start; `close` is the price one hour later, so the 09:15 bar fills ~10:15.
Sizing follows Strategy 1 (9% of notional margin per lot, 60% of equity used, lot 65, Rs 5L start).

Usage:  python research_monthly_straddle.py
Writes: data/reports/monthly_straddle_summary.csv, monthly_straddle_trades.csv (base variant)
"""
import calendar
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from cost_model import MONTHLY_RUNNING_COSTS, option_fill_cost, slip
from engine import load_daily

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "data" / "raw" / "hourly_parquet" / "options_monthly"
OUT = ROOT / "data" / "reports"

LOT = 65
MARGIN_PCT = 0.09
MAX_MARGIN_USE = 0.60
START_CAPITAL = 500_000
RUNNING_PER_MONTH = sum(MONTHLY_RUNNING_COSTS.values())

ENTRY_DTE = 45
EXIT_DTE = 21
ENTRY_TIME = "09:15"      # bar start; fills at its close (~10:15)
SPOT_TOL = 0.01           # a row's spot must be within 1% of the bar's median spot
IS_END = "2023-12-31"     # parameters are judged on 2021-2023; 2024+ is out-of-sample
LINEAR_SKEW = True        # model pricing: extrapolate IV linearly in strike (False = flat from the edge strike)


# ---------------------------------------------------------------- data
@dataclass
class Chain:
    spot: float
    px: dict                      # (strike, "CE"|"PE") -> close
    _fwd: float = field(default=None)

    def forward(self):
        """Forward from put-call parity at the strikes nearest spot (median of up to 3)."""
        if self._fwd is None:
            both = sorted((k for (k, t) in self.px if t == "CE" and (k, "PE") in self.px),
                          key=lambda k: abs(k - self.spot))[:3]
            if both:
                self._fwd = float(np.median([k + self.px[(k, "CE")] - self.px[(k, "PE")] for k in both]))
        return self._fwd


def monthly_expiries(daily, last_date):
    """Monthly expiry dates (YYYY-MM-DD), detected from the data, extended past `last_date`."""
    exp_days = daily.index[daily["is_expiry"].fillna(False).astype(bool)]
    last_full = daily.index[-1].to_period("M")          # the final month may be incomplete
    by_month = pd.Series(exp_days, index=exp_days.to_period("M")).groupby(level=0).max()
    out = [d.date() for p, d in by_month.items() if p < last_full]
    wd = out[-1].weekday()
    horizon = date.fromisoformat(last_date) + timedelta(days=100)
    y, m = out[-1].year, out[-1].month
    while out[-1] < horizon:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        d = date(y, m, calendar.monthrange(y, m)[1])
        while d.weekday() != wd:
            d -= timedelta(days=1)
        out.append(d)
    return [str(d) for d in out]


def load_contracts():
    """-> {expiry: {timestamp: Chain}}, expiries list."""
    # About 5% of next-month rows carry a wrong `strike` (and often `spot`) while `close` is the
    # price of the labelled offset. Keep a row only if its strike equals ATM(spot) + 50 * offset
    # and its spot agrees with the other rows of that bar.
    d = (pl.read_parquet(str(SRC / "year=*" / "month=*" / "options.parquet"))
           .filter(pl.col("close") > 0)
           .with_columns(pl.col("spot").median().over("timestamp").alias("spot_med"))
           .filter((pl.col("strike") == (pl.col("spot") / 50).round() * 50 + pl.col("strike_offset") * 50)
                   & ((pl.col("spot") / pl.col("spot_med") - 1).abs() <= SPOT_TOL))
           .select("timestamp", "date", "expiry_code", "strike", "option_type", "spot", "close")
           .to_pandas())
    exps = monthly_expiries(load_daily(), d["date"].max())
    exp_arr = np.array(exps)
    i = np.searchsorted(exp_arr, d["date"].to_numpy(), side="left")     # first expiry >= date
    d["expiry"] = exp_arr[i + d["expiry_code"].to_numpy() - 1]
    d["strike"] = d["strike"].astype(int)
    chains = {}
    for (e, ts), g in d.groupby(["expiry", "timestamp"], sort=True):
        chains.setdefault(e, {})[ts] = Chain(float(g["spot"].median()),
                                             dict(zip(zip(g["strike"], g["option_type"]), g["close"])))
    return chains, exps


# ---------------------------------------------------------------- Black-76
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black(F, K, T, iv, typ):
    if T <= 0 or iv <= 0:
        return max(F - K, 0.0) if typ == "CE" else max(K - F, 0.0)
    s = iv * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    c = F * _ncdf(d1) - K * _ncdf(d1 - s)
    return c if typ == "CE" else c - F + K


def implied(F, K, T, price, typ):
    if T <= 0 or price <= black(F, K, T, 0, typ) + 0.05:
        return None
    lo, hi = 0.01, 3.0
    if price >= black(F, K, T, hi, typ):
        return None
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if black(F, K, T, mid, typ) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def t_years(ts, expiry):
    end = pd.Timestamp(expiry) + pd.Timedelta(hours=15, minutes=30)
    return max((end - (ts + pd.Timedelta(hours=1))).total_seconds(), 0.0) / (365 * 86400)


def leg_price(chain, ts, expiry, K, typ):
    """-> (price, how) with how in real / parity / model, or (None, None) if the bar cannot be priced."""
    q = chain.px.get((K, typ))
    if q is not None:
        return q, "real"
    F = chain.forward()
    if F is None:
        return None, None
    other = chain.px.get((K, "PE" if typ == "CE" else "CE"))
    intrinsic = max(F - K, 0.0) if typ == "CE" else max(K - F, 0.0)
    if other is not None:
        return max(other + (F - K if typ == "CE" else K - F), intrinsic), "parity"
    T = t_years(ts, expiry)
    side = "CE" if K >= F else "PE"                     # the OTM type at this strike
    pts = []
    for (k, t), p in chain.px.items():
        if t == side and ((k > F) if side == "CE" else (k < F)):
            iv = implied(F, k, T, p, side)
            if iv is not None:
                pts.append((abs(k - K), k, iv))
    if not pts:
        return None, None
    pts.sort()
    pts = pts[:6]
    edge_iv = pts[0][2]
    iv = edge_iv
    if len(pts) >= 3 and LINEAR_SKEW:
        slope, icpt = np.polyfit([p[1] for p in pts], [p[2] for p in pts], 1)
        iv = float(np.clip(slope * K + icpt, 0.6 * edge_iv, 1.8 * edge_iv))
    otm = black(F, K, T, iv, side)
    return (otm if typ == side else otm + abs(F - K)), "model"


# ---------------------------------------------------------------- one trade
def run_trade(chain, expiry, entry_date, exit_date, tp=None, stop=None):
    """Short straddle at the strike nearest the forward. tp / stop are fractions of the credit."""
    bars = [ts for ts in chain if str(ts.date()) >= entry_date]
    entry = None
    for ts in bars:
        if str(ts.date()) > entry_date:
            break
        c = chain[ts]
        if ts.strftime("%H:%M") < ENTRY_TIME or c.forward() is None:
            continue
        both = [k for (k, t) in c.px if t == "CE" and (k, "PE") in c.px]
        if both:
            K = min(both, key=lambda k: abs(k - c.forward()))
            entry = ts
            break
    if entry is None:
        return None
    c = chain[entry]
    spot0, fwd0 = c.spot, c.forward()
    ce, pe = c.px[(K, "CE")], c.px[(K, "PE")]
    credit = (ce - slip(ce)) + (pe - slip(pe))
    d0 = str(entry.date())
    cost = option_fill_cost(ce, LOT, False, d0) + option_fill_cost(pe, LOT, False, d0)

    mtm, worst = {}, 0.0
    for ts in bars:
        if ts <= entry:
            continue
        c = chain[ts]
        (pc, hc), (pp, hp) = leg_price(c, ts, expiry, K, "CE"), leg_price(c, ts, expiry, K, "PE")
        if pc is None or pp is None:
            continue
        d = str(ts.date())
        pnl_mid = credit - pc - pp
        mtm[d] = pnl_mid
        worst = min(worst, pnl_mid)
        reason = None
        if d >= exit_date and ts.strftime("%H:%M") >= ENTRY_TIME:
            reason = "time"
        elif stop is not None and pnl_mid <= -stop * credit:
            reason = "stop"
        elif tp is not None and pnl_mid - slip(pc) - slip(pp) >= tp * credit:
            reason = "tp"
        if reason:
            urgent = reason == "stop"
            close = sum(p + slip(p, urgent or h == "model") for p, h in ((pc, hc), (pp, hp)))
            pts = credit - close
            cost += option_fill_cost(pc, LOT, True, d) + option_fill_cost(pp, LOT, True, d)
            mtm[d] = pts
            return {"expiry": expiry, "entry": d0, "exit": d, "reason": reason, "strike": K,
                    "spot": spot0, "fwd": round(fwd0, 1),
                    "entry_dte": (date.fromisoformat(expiry) - date.fromisoformat(d0)).days,
                    "exit_dte": (date.fromisoformat(expiry) - date.fromisoformat(d)).days,
                    "exit_spot": c.spot, "credit": credit, "pts": pts, "worst_pts": worst,
                    "px_ce": hc, "px_pe": hp, "cost": cost, "rs": pts * LOT - cost, "mtm": mtm}
    return None                                         # still open when the data ends


def backtest(chains, exps, tp=None, stop=None, roll=False):
    """roll=False: enter each contract at <=45 DTE. roll=True: enter the next contract the day the
    previous one reaches 21 DTE (49 or 56 DTE), so the book is always on."""
    rows, prev_exit_date = [], None
    for e in exps:
        if e not in chains:
            continue
        sessions = sorted({str(ts.date()) for ts in chains[e]})
        dte = lambda d: (date.fromisoformat(e) - date.fromisoformat(d)).days
        exit_date = next((d for d in sessions if dte(d) <= EXIT_DTE), None)
        entry_date = next((d for d in sessions if dte(d) <= ENTRY_DTE), None)
        if roll and prev_exit_date is not None and prev_exit_date in sessions:
            entry_date = prev_exit_date
        scheduled = exit_date
        if entry_date and exit_date and dte(entry_date) >= ENTRY_DTE - 4:
            t = run_trade(chains[e], e, entry_date, exit_date, tp, stop)
            if t:
                rows.append(t)
        prev_exit_date = scheduled
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- sizing and metrics
def simulate(df, start=START_CAPITAL):
    """-> per-trade ledger, daily mark-to-market equity series."""
    eq, last_month, rows, curve = start, None, [], {}
    for r in df.itertuples():
        month = pd.Period(r.entry[:7], "M")              # running costs accrue in idle months too
        fixed = RUNNING_PER_MONTH * (1 if last_month is None else (month - last_month).n)
        last_month = month
        lots = int(MAX_MARGIN_USE * eq // (MARGIN_PCT * r.spot * LOT))
        for d, pts in r.mtm.items():
            curve[d] = eq + lots * pts * LOT
        pnl = r.rs * lots - fixed
        rows.append({"expiry": r.expiry, "entry": r.entry, "exit": r.exit, "reason": r.reason,
                     "entry_dte": r.entry_dte, "exit_dte": r.exit_dte, "strike": r.strike,
                     "spot": round(r.spot, 1), "exit_spot": round(r.exit_spot, 1), "lots": lots,
                     "credit_pts": round(r.credit, 1), "pnl_pts": round(r.pts, 1),
                     "worst_mtm_pts": round(r.worst_pts, 1), "px_ce": r.px_ce, "px_pe": r.px_pe,
                     "stat_costs": round(r.cost * lots), "fixed_costs": fixed, "pnl": round(pnl),
                     "ret_pct": pnl / eq * 100, "equity": round(eq + pnl)})
        eq += pnl
        curve[r.exit] = eq
    return pd.DataFrame(rows), pd.Series(curve).sort_index()


def metrics(led, curve, start=START_CAPITAL):
    yrs = (pd.Timestamp(led["exit"].iloc[-1]) - pd.Timestamp(led["entry"].iloc[0])).days / 365.25
    eq = pd.concat([pd.Series([start]), curve]).reset_index(drop=True)
    wins, losses = led[led.pnl > 0], led[led.pnl <= 0]
    modelled = ((led.px_ce == "model") | (led.px_pe == "model")).mean()
    m = {"trades": len(led), "years": round(yrs, 2), "final_equity": int(eq.iloc[-1]),
         "cagr_pct": round(((eq.iloc[-1] / start) ** (1 / yrs) - 1) * 100, 1) if eq.iloc[-1] > 0 else -100.0,
         "max_dd_pct": round((eq / eq.cummax() - 1).min() * 100, 1),
         "win_rate_pct": round(len(wins) / len(led) * 100, 1),
         "avg_pts": round(led.pnl_pts.mean(), 1),
         "profit_factor": round(wins.pnl.sum() / -losses.pnl.sum(), 2) if len(losses) else float("inf"),
         "worst_trade_pct": round(led.ret_pct.min(), 1), "best_trade_pct": round(led.ret_pct.max(), 1),
         "n_tp": int((led.reason == "tp").sum()), "n_stop": int((led.reason == "stop").sum()),
         "model_exit_pct": round(modelled * 100, 1)}
    return {k: float(v) if isinstance(v, np.floating) else v for k, v in m.items()}


def evaluate(df):
    out = {}
    for name, sub in (("all", df), ("is", df[df.entry <= IS_END]), ("oos", df[df.entry > IS_END])):
        if len(sub) == 0:
            continue
        led, curve = simulate(sub)
        m = metrics(led, curve)
        out.update({f"{name}_{k}": v for k, v in m.items()} if name != "all" else m)
    return out


def main():
    chains, exps = load_contracts()
    print(f"{sum(len(c) for c in chains.values()):,} contract-bars, {len(chains)} monthly contracts "
          f"{min(chains)} .. {max(chains)}")

    variants = [dict(tp=tp, stop=stop, roll=roll)
                for roll in (False, True) for tp in (None, 0.25, 0.50) for stop in (None, 1.0, 2.0)]
    rows = []
    for v in variants:
        df = backtest(chains, exps, **v)
        rows.append({**v, **evaluate(df)})
    summ = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    summ.to_csv(OUT / "monthly_straddle_summary.csv", index=False)
    cols = ["roll", "tp", "stop", "trades", "n_tp", "n_stop", "cagr_pct", "max_dd_pct", "win_rate_pct", "avg_pts",
            "profit_factor", "worst_trade_pct", "model_exit_pct",
            "is_cagr_pct", "is_max_dd_pct", "oos_cagr_pct", "oos_max_dd_pct"]
    print(summ[cols].to_string(index=False))

    base, curve = simulate(backtest(chains, exps))
    base.to_csv(OUT / "monthly_straddle_trades.csv", index=False)
    print("\nBase variant (45 -> 21 DTE, no target, no stop):", metrics(base, curve))
    print(base.assign(year=base.entry.str[:4]).groupby("year")
              .agg(trades=("pnl", "size"), pnl_pts=("pnl_pts", "sum"), pnl=("pnl", "sum"),
                   worst_pts=("pnl_pts", "min"), wins=("pnl", lambda s: (s > 0).sum())).to_string())


if __name__ == "__main__":
    main()
