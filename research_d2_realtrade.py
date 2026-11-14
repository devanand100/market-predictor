"""
D2 (short +/-1x straddle strangle, delta-hedged with Nifty futures, 1-lot band) re-checked on REAL
prices (2026-11-14). The 26.3% in docs/INSTITUTIONAL_STRATEGIES.md comes from research_institutional.py,
whose Day.px prices any strike without a traded price at intrinsic value; that pricing inflated L2
from PF 1.40 to 1.03 (docs/STRATEGY_6_REALTRADE.md). This is a verification, not a tuning pass:
D2's rules are unchanged, all three bands are reported, 2021-23 / 2024+ split as before.

Rows, each adding one change to the one above (weekly return on margin capital, 10 lots):
  R0  old engine pricing, lot 65 throughout (must reproduce research_institutional: 26.4%)
  R1  real prices only (research_realtrade.real): entry needs a traded price on both legs at 09:30,
      else the week is skipped; marks keep the last real price; a leg with no real price at exit
      is bought back at max(last mark, intrinsic) + urgent slippage (counted as unobserved)
  R2  + NSE lot size of the date (75 / 50 / 25 / 75 / 65), so the 1-lot hedge band is that era's lot
  R3  + margin 11.6% of notional (Dhan's real strangle margin, STRATEGY_6_REALTRADE.md section 9)
      instead of 9%, same 60% utilisation (the 40% left covers futures margin and MTM): a scaling of
      returns, not of Sharpe
Stresses on R2 (unchanged from research_institutional_stress.py): futures slippage 2 / 3 pts,
basis drift 5 pts a day against the hedge, all three (slip 2 + basis 5 + ATM IV). Bands 2 and 4 lots.

Account: Rs 10L / 15L / 25L, compounding, whole lots of the date at 11.6% margin and 60% use, Rs 1,990
a month running costs, monthly rupees; base and pessimistic (slip 2 + basis 5).

Synthetic 2019-11..2020-12 (research_synthetic_options.py, Black-76 on modelled minute VIX with the IV
surface fitted on real 2021-23 prices): the COVID crash D2's real data never saw. SynthDay has no IV
columns, so the hedge's delta uses the model's own sigma for each strike (the IV it priced with).
Base, and option slippage x3 with futures slippage 3 pts. Only with the `syn` argument (needs scipy);
not run on 2026-11-14 because D2 already failed on real 2021-26 prices.

Writes data/reports/d2_realtrade_summary.csv, d2_realtrade_weeks.csv, d2_realtrade_account.csv
"""
import math
import sys

import numpy as np
import pandas as pd

import cost_model
import research_institutional as ri
import research_positional as rp
from cost_model import get_lot_size, option_fill_cost, MONTHLY_RUNNING_COSTS
from engine import load_days, load_daily, atm_strike
from research_realtrade import real

REP = "data/reports/"
REAL_MARGIN, OLD_MARGIN, UTIL = 0.116, 0.09, 0.60
RUNNING = sum(MONTHLY_RUNNING_COSTS.values())
SLIP_MULT = 1.0


def slip(p, urgent=False):
    return SLIP_MULT * cost_model.slip(p, urgent)


class RealBook(ri.Book):
    def __init__(self, date, real_only):
        super().__init__(date)
        self.real_only = real_only
        self.unobserved = 0

    def price(self, day, K, typ, m):
        return real(day, K, typ, m) if self.real_only else day.px(K, typ, m)

    def open(self, day, typ, K, qty, m):
        p = self.price(day, K, typ, m)
        fill = p + slip(p) if qty > 0 else p - slip(p)
        self.costs += option_fill_cost(fill, abs(qty), qty > 0, self.date)
        self.legs.append({"typ": typ, "K": K, "qty": qty, "entry": fill, "mark": p})
        return fill

    def close(self, day, leg, m, urgent=False):
        p = self.price(day, leg["K"], leg["typ"], m)
        if p is None:
            self.unobserved += 1
            s = day.spot[m]
            intr = max(0.0, (s - leg["K"]) if leg["typ"] == "CE" else (leg["K"] - s))
            if leg["qty"] < 0:
                base = max(leg["mark"], intr)
                fill = base + slip(base, True)
            else:
                fill = max(0.05, intr - slip(intr, True))
        else:
            fill = p - slip(p, urgent) if leg["qty"] > 0 else p + slip(p, urgent)
            fill = max(0.05, fill)
        self.costs += option_fill_cost(fill, abs(leg["qty"]), leg["qty"] < 0, self.date)
        self.realized += leg["qty"] * (fill - leg["entry"])
        self.legs.remove(leg)

    def update_marks(self, day, m):
        for leg in self.legs:
            p = self.price(day, leg["K"], leg["typ"], m)
            if p is not None:
                leg["mark"] = p

    def delta(self, day, m, T):
        if hasattr(day, "ce_iv"):
            return super().delta(day, m, T)
        # synthetic day: the model's own sigma for each strike
        import research_synthetic_options as rso
        S, mdl = day.spot[m], day.model
        Ts = rso.t_years(day.dte, m)
        F = S * math.exp(mdl.r * Ts)
        v = float(day.v[m])
        tot = self.fut
        for leg in self.legs:
            z = math.log(leg["K"] / F) / (v / 100 * math.sqrt(Ts))
            sig = v * float(mdl.ratio(day.dte, m, v, np.array(z)))
            tot += leg["qty"] * ri.bs_delta(S, leg["K"], sig, T, leg["typ"])
        return tot


def run_week(days, window, lots=10, band_lots=1, real_only=True, lot_size=None, margin=OLD_MARGIN):
    exp = window[-1]
    d0 = days[window[0]]
    m0 = ri.ENTRY_MIN
    lot = lot_size or get_lot_size(window[0])
    q = lots * lot
    spot0 = d0.spot[m0]
    book = RealBook(window[0], real_only)
    K = atm_strike(spot0)
    ce, pe = book.price(d0, K, "CE", m0), book.price(d0, K, "PE", m0)
    if ce is None or pe is None:
        return {"entry": window[0], "skipped": "no ATM price"}
    w = ri.width(ce + pe, 1.0)
    pc, pp = book.price(d0, K + w, "CE", m0), book.price(d0, K - w, "PE", m0)
    if pc is None or pp is None:
        return {"entry": window[0], "skipped": "no strangle price"}
    c = book.open(d0, "CE", K + w, -q, m0)
    p = book.open(d0, "PE", K - w, -q, m0)
    if c < 1.0 or p < 1.0:
        return {"entry": window[0], "skipped": "premium < 1"}
    credit = (c + p) * q
    prev_i = 0
    for i, date, m in ri.minutes_path(window):
        day = days[date]
        if i != prev_i:
            book.costs += abs(book.fut) * ri.BASIS_PTS_PER_DAY
            prev_i = i
        if date == exp and m >= ri.EXIT_MIN:
            break
        book.update_marks(day, m)
        net = book.delta(day, m, ri.t_years(date, m, exp))
        if abs(net) > band_lots * lot:
            book.trade_fut(day.spot[m], int(round(-net / lot)) * lot)
    book.close_all(days[exp], ri.EXIT_MIN)
    pnl = book.realized - book.costs
    capital = margin * spot0 * q / UTIL
    return {"entry": window[0], "exit": exp, "pnl": pnl, "ret_pct": pnl / capital * 100, "credit_rs": credit,
            "costs": book.costs, "hedges": book.n_hedges, "unobserved": book.unobserved, "lots": lots,
            "lot": lot, "spot": spot0, "skipped": None, "adj": 0}


def summarize(T, name):
    ok = T[T["skipped"].isna()].copy()
    s = ri.summarize(ok.reset_index(drop=True), name)
    s["weeks"] = len(ok)
    s["skipped"] = int(T["skipped"].notna().sum())
    s["unobserved legs"] = int(ok["unobserved"].sum())
    return s


def set_stress(slip_pts=1.0, basis=0.0, atm=False, slip_mult=1.0):
    global SLIP_MULT
    cost_model.FUT_SLIP_PTS, ri.BASIS_PTS_PER_DAY, ri.ATM_IV_ONLY, SLIP_MULT = slip_pts, basis, atm, slip_mult


def weeks(days, cyc, **kw):
    return pd.DataFrame([run_week(days, w, **kw) for w, e in cyc])


def account(days, cyc, start, margin=REAL_MARGIN):
    eq, rows = float(start), []
    months_charged = set()
    for w, e in cyc:
        mon = w[0][:7]
        if mon not in months_charged:
            months_charged.add(mon)
            eq -= RUNNING
            rows.append({"entry": w[0], "exit": w[0], "pnl": -RUNNING, "lots": 0, "kind": "running"})
        d0 = days[w[0]]
        lot = get_lot_size(w[0])
        lots = int(eq * UTIL // (margin * d0.spot[ri.ENTRY_MIN] * lot))
        if lots < 1:
            continue
        r = run_week(days, w, lots=lots, margin=margin)
        if r["skipped"]:
            continue
        eq += r["pnl"]
        rows.append({"entry": w[0], "exit": e, "pnl": r["pnl"], "lots": lots, "kind": "week", "equity": eq})
    A = pd.DataFrame(rows)
    A["equity"] = start + A["pnl"].cumsum()
    return A


def show_account(A, start, name):
    yrs = (pd.Timestamp(A["exit"].iloc[-1]) - pd.Timestamp(A["entry"].iloc[0])).days / 365.25
    eq = A["equity"]
    peak = np.maximum.accumulate(np.r_[start, eq.to_numpy()])[1:]
    monthly = A.groupby(A["exit"].str[:7])["pnl"].sum()
    wk = A[A["kind"] == "week"]
    lots = wk.groupby(wk["entry"].str[:4])["lots"].mean().round(1).to_dict()
    yearly = A.groupby(A["exit"].str[:4])["pnl"].sum().round(-3).to_dict()
    late = monthly[monthly.index >= "2024-01"]
    out = {"account": name, "CAGR": round(((eq.iloc[-1] / start) ** (1 / yrs) - 1) * 100, 1),
           "DD": round(((eq / peak) - 1).min() * 100, 1), "avg month Rs": round(monthly.mean()),
           "2024+ avg month Rs": round(late.mean()), "median month Rs": round(monthly.median()),
           "months up %": round((monthly > 0).mean() * 100), "worst month Rs": round(monthly.min())}
    print(out)
    print("   lots a week by year:", lots)
    print("   P&L by year (Rs):", yearly)
    return out


def main():
    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 30)
    days, daily = load_days(), load_daily()
    cyc = rp.cycles(daily, days)
    rows, allw = [], []

    def add(name, T):
        T = T.assign(row=name)
        allw.append(T)
        rows.append(summarize(T, name))
        print(rows[-1], flush=True)

    set_stress()
    add("R0 old engine pricing, lot 65", weeks(days, cyc, real_only=False, lot_size=65))
    add("R1 real prices, lot 65", weeks(days, cyc, lot_size=65))
    R2 = weeks(days, cyc)
    add("R2 real prices, lot of the date", R2)
    add("R3 R2 at 11.6% margin", R2.assign(ret_pct=R2["ret_pct"] * OLD_MARGIN / REAL_MARGIN))
    for band in (2, 4):
        add(f"R2, band {band} lots", weeks(days, cyc, band_lots=band))
    for name, kw in [("R2, futures slippage 2 pts", dict(slip_pts=2.0)),
                     ("R2, futures slippage 3 pts", dict(slip_pts=3.0)),
                     ("R2, basis 5 pts/day", dict(basis=5.0)),
                     ("R2, all three: slip 2 + basis 5 + ATM IV", dict(slip_pts=2.0, basis=5.0, atm=True))]:
        set_stress(**kw)
        add(name, weeks(days, cyc))
    set_stress()
    S = pd.DataFrame(rows)
    S.to_csv(REP + "d2_realtrade_summary.csv", index=False)
    W = pd.concat(allw)
    W.to_csv(REP + "d2_realtrade_weeks.csv", index=False)
    print(S.to_string(index=False))
    print("\nskipped weeks (R2):", R2["skipped"].value_counts().to_dict())
    r2 = R2[R2["skipped"].isna()]
    by = r2.groupby(r2["entry"].str[:4])["ret_pct"].apply(lambda r: ((1 + r / 100).prod() - 1) * 100)
    print("R2 by year %:", by.round(1).to_dict())
    print("R2 worst weeks:")
    print(r2.nsmallest(5, "ret_pct")[["entry", "exit", "ret_pct", "hedges", "unobserved"]].to_string(index=False))

    print("\nACCOUNTS (11.6% margin, 60% use, whole lots of the date, Rs 1,990/month)")
    acc = []
    for start in (1_000_000, 1_500_000, 2_500_000):
        for name, kw in [("base", {}), ("slip 2 + basis 5", dict(slip_pts=2.0, basis=5.0))]:
            set_stress(**kw)
            A = account(days, cyc, start)
            acc.append(show_account(A, start, f"Rs {start / 1e5:.0f}L {name}"))
    set_stress()
    pd.DataFrame(acc).to_csv(REP + "d2_realtrade_account.csv", index=False)

    if "syn" in sys.argv:          # needs scipy (research_synthetic_options); not run 2026-11-14
        synthetic()


def synthetic():
    import research_synthetic_options as rso
    print("\nSYNTHETIC 2019-11..2020-12 (model prices; delta from the model's sigma)")
    daily = rso.daily_frame()
    syn = rso.synth_days(rso.trading_dates_pre2021(), daily, rso.Model())
    cyc = rp.cycles(daily, syn)
    sp = rso.spot_minutes()
    for d in ("2020-03-13", "2020-03-23"):
        a = sp.get(d)
        if a is not None:
            c = pd.Series(a[:, 3])
            flat = (c.diff() == 0).astype(int)
            run_len = flat.groupby((flat == 0).cumsum()).sum().max()
            print(f"  {d}: spot 09:15 {c.iloc[0]:.0f}, low {np.nanmin(a[:, 2]):.0f}, close {c.iloc[-1]:.0f}; "
                  f"longest unchanged-close run {run_len} minutes (circuit halt)")
    rows = []
    for name, kw in [("synthetic base", {}), ("synthetic slip x3, futures 3 pts", dict(slip_pts=3.0, slip_mult=3.0))]:
        set_stress(**kw)
        T = weeks(syn, cyc)
        s = summarize(T, name)
        rows.append(s)
        print(s)
        ok = T[T["skipped"].isna()]
        crash = ok[(ok["entry"] >= "2020-02-15") & (ok["entry"] <= "2020-04-30")]
        print("  Feb 15 - Apr 30 2020 weeks %:", dict(zip(crash["entry"], crash["ret_pct"].round(1))))
    set_stress()
    pd.DataFrame(rows).to_csv(REP + "d2_realtrade_synthetic.csv", index=False)


if __name__ == "__main__":
    main()
