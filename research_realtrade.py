"""
Real-market replay backtest: one account, traded day by day and minute by minute on the Dhan
1-minute option data in data/processed (engine cache), with no synthetic prices.

What "real" means here (differences from the research scripts):
  * Fills only at REAL traded prices of that contract at that minute (Dhan close = LTP, the engine
    keeps at most 5 minutes of LTP carry inside the +/-500 window). No put-call parity, no
    Black-Scholes/Black-76, no intrinsic-value pricing of legs that are still trading.
    A position is only opened when every leg has a real price at the entry minute.
  * If a leg has no real price when it must be closed (it drifted outside the stored strike
    window), the fill is CONSERVATIVE: a short is bought back at max(last real price, intrinsic)
    plus urgent slippage, a long is sold at intrinsic minus slippage. Counted in `unobserved_fills`.
  * Lot size is the NSE lot size of the trade date (75 -> 50 -> 25 -> 75 -> 65), whole lots only,
    sized on account equity (cash + open MTM) at the decision minute; compounding.
  * Statutory costs per order at the traded quantity, slippage on every fill (urgent on stops),
    a resting limit target fills at the limit only when the 1-minute high trades through it.
  * Rs 1,990/month running costs (algo: data API + VPS) charged on the first session of each month.
  * Equity is marked to market every session close on real prices, so max drawdown includes open
    losses; profit factor is per closed trade (gross wins / gross losses, net of costs).
  * Signals use only information available at the decision minute (previous session VIX close, gap at 09:30,
    previous session close). Parameters are judged on 2021-2023; 2024+ is out-of-sample.

Writes data/reports/realtrade_summary.csv, realtrade_trades.csv, realtrade_equity.csv
"""
import itertools
import json
import sys

import numpy as np
import pandas as pd

from engine import load_days, load_daily, atm_strike, NMIN
from cost_model import option_fill_cost, slip, get_lot_size, MONTHLY_RUNNING_COSTS

ENTRY_MIN = 15          # 09:30
EXIT_MIN = 360          # 15:15
MARGIN_PCT = 0.09       # margin per short strangle lot, fraction of notional (SPAN + exposure, offset)
WINDOW_MAX = 450        # never sell a strike further than this from ATM (stored window is +/-500)
MAX_UTIL = 0.90         # never let total margin exceed 90% of equity
RUNNING = sum(MONTHLY_RUNNING_COSTS.values())
IS_END = "2023-12-31"
SYNTHETIC_FALLBACK = False   # True = old engine behaviour: a strike with no real price is priced at intrinsic


def margin_pct(date, expiry_day):
    """Naked short margin per lot as a fraction of notional; SEBI added 2% ELM on expiry day from 2024-11-20."""
    return MARGIN_PCT + (0.02 if expiry_day and date >= "2024-11-20" else 0.0)


def real(day, K, typ, m, high=False):
    j = day.sidx.get(float(K))
    if j is None:
        return _intrinsic(day, K, typ, m) if SYNTHETIC_FALLBACK else None
    a = (day.ce_hi if high else day.ce) if typ == "CE" else (day.pe_hi if high else day.pe)
    v = a[m, j]
    if v != v:
        return _intrinsic(day, K, typ, m) if SYNTHETIC_FALLBACK else None
    return float(v)


def _intrinsic(day, K, typ, m):
    s = day.spot[m]
    return max(0.05, (s - K) if typ == "CE" else (K - s))


# ------------------------------------------------------------------ account
class Account:
    def __init__(self, start, running=True):
        self.cash = float(start)
        self.legs = []                 # open legs
        self.trades = []               # trade groups (open and closed)
        self.open = []                 # open trade groups
        self.running = running
        self.unobserved = 0
        self.date = None

    # --- marks / equity
    def open_mtm(self):
        return sum(l["qty"] * (l["mark"] - l["entry"]) for l in self.legs) + sum(t.get("vmark", 0.0) for t in self.open)

    def equity(self):
        return self.cash + self.open_mtm()

    def update_marks(self, day, m):
        for l in self.legs:
            p = real(day, l["K"], l["typ"], m)
            if p is not None:
                l["mark"] = p

    def margin_used(self):
        return sum(t["meta"].get("margin", 0.0) for t in self.open)

    # --- orders
    def _cost(self, fill, units, is_buy):
        c = option_fill_cost(fill, units, is_buy, self.date)
        self.cash -= c
        return c

    def open_group(self, day, m, sleeve, specs, meta=None):
        """specs: list of (typ, K, qty_signed). All legs must have a real price at m."""
        pxs = [real(day, K, typ, m) for typ, K, q in specs]
        if any(p is None for p in pxs):
            return None
        t = dict(sleeve=sleeve, entry=self.date, entry_min=m, legs=[], pnl=0.0, costs=0.0,
                 open=True, credit=0.0, meta=meta or {}, exit=None, reason=None)
        for (typ, K, q), p in zip(specs, pxs):
            fill = p + slip(p) if q > 0 else p - slip(p)
            if q < 0 and fill < 0.5:
                pass
            t["costs"] += self._cost(fill, abs(q), q > 0)
            leg = dict(typ=typ, K=K, qty=q, entry=fill, mark=p)
            t["legs"].append(leg)
            t["credit"] -= q * fill
            self.legs.append(leg)
        t["pnl"] -= t["costs"]
        self.trades.append(t)
        self.open.append(t)
        return t

    def close_leg(self, t, leg, day, m, urgent=False, limit=None):
        p = real(day, leg["K"], leg["typ"], m)
        if limit is not None:
            fill = limit
        elif p is None:
            self.unobserved += 1
            s = day.spot[m]
            intr = max(0.0, (s - leg["K"]) if leg["typ"] == "CE" else (leg["K"] - s))
            if leg["qty"] < 0:
                fill = max(leg["mark"], intr) + slip(max(leg["mark"], intr), True)
            else:
                fill = max(0.05, intr - slip(intr, True))
        else:
            fill = p + slip(p, urgent) if leg["qty"] < 0 else max(0.05, p - slip(p, urgent))
        c = self._cost(fill, abs(leg["qty"]), leg["qty"] < 0)
        g = leg["qty"] * (fill - leg["entry"])
        self.cash += g
        t["pnl"] += g - c
        t["costs"] += c
        leg["mark"] = fill
        self.legs.remove(leg)

    def close_group(self, t, day, m, reason, urgent=False, limit=None):
        for leg in [l for l in t["legs"] if l in self.legs]:
            self.close_leg(t, leg, day, m, urgent, limit)
        t["open"] = False
        self.open.remove(t)
        t["exit"], t["exit_min"], t["reason"] = self.date, m, reason

    def group_mtm(self, t):
        return sum(l["qty"] * (l["mark"] - l["entry"]) for l in t["legs"])


# ------------------------------------------------------------------ calendar
def calendar(days, daily):
    """Per trading date with options data: (i = session index in its weekly cycle, n = sessions in cycle,
    expiry date). Cycles run from the session after one expiry through the next expiry."""
    tds = [d for d in daily.index.strftime("%Y-%m-%d") if d in days]
    exp = set(daily.index[daily["is_expiry"] == True].strftime("%Y-%m-%d"))
    info, cur = {}, []
    for d in tds:
        cur.append(d)
        if d in exp:
            for i, x in enumerate(cur):
                info[x] = (i, len(cur), d)
            cur = []
    return info, tds


# ------------------------------------------------------------------ strategies
class StaggeredStrangle:
    """L2: sell 1/3 of the size as a strangle at ATM +/- straddle on each of the first 3 sessions
    of the weekly cycle at 09:30; each tranche stops at loss >= stop x its credit; else 15:15 expiry."""
    sleeve = "L2"

    def __init__(self, weight=0.30, stop=1.5, mult=1.0, tranches=3, min_lots=1, skip_vix_above=None,
                 tp=None, check_every=1, min_total=1):
        self.w, self.stop, self.mult, self.n, self.min_lots = weight, stop, mult, tranches, min_lots
        self.k, self.min_total = check_every, min_total
        self.skip_vix_above, self.tp = skip_vix_above, tp
        self.lots = 0

    def on_minute(self, acct, day, m, cal, dctx):
        i, n, exp = cal
        if m == ENTRY_MIN and i < self.n and i < n - 1:
            if i == 0:
                lot = get_lot_size(acct.date)
                spot = day.spot[m]
                total = int(self.w * acct.equity() // (MARGIN_PCT * spot * lot))
                self.lots = total // self.n if (total // self.n >= self.min_lots and total >= self.min_total) else 0
                self.cycle_ok = not (self.skip_vix_above and dctx["vix"] > self.skip_vix_above)
            if self.lots and self.cycle_ok:
                S0 = day.spot[m]
                K = atm_strike(S0)
                ce, pe = real(day, K, "CE", m), real(day, K, "PE", m)
                if ce is not None and pe is not None:
                    w = min(WINDOW_MAX, max(50, int(round(self.mult * (ce + pe) / 50)) * 50))
                    q = self.lots * get_lot_size(acct.date)
                    t = acct.open_group(day, m, self.sleeve, [("CE", K + w, -q), ("PE", K - w, -q)],
                                        dict(width=w, lots=self.lots, spot=S0, exp=exp))
        for t in [t for t in acct.open if t["sleeve"] == self.sleeve]:
            if acct.date == t["meta"]["exp"] and m >= EXIT_MIN:
                acct.close_group(t, day, m, "expiry")
                continue
            if t["entry"] == acct.date and m <= t["entry_min"]:
                continue
            if m % self.k:
                continue
            g = acct.group_mtm(t)
            if g <= -self.stop * t["credit"]:
                acct.close_group(t, day, m, "stop", urgent=True)
            elif self.tp and g >= self.tp * t["credit"]:
                acct.close_group(t, day, m, "target")


class GapBuy:
    """Strategy 3: on the session 4 before expiry, 09:30, buy the ATM CE (gap up) or PE (gap down) of
    the weekly expiry when VIX(09:20) <= vix_max; premium = frac x equity in whole lots; resting
    limit sell at (1 + tp) x fill; else sell 15:15 on expiry day."""
    sleeve = "GAP"

    def __init__(self, frac=0.03, vix_max=15, tp=1.0, dte=4, min_gap=0.0, target="limit", vix_src="prev",
                 check_every=1):
        self.frac, self.vix_max, self.tp, self.dte, self.min_gap = frac, vix_max, tp, dte, min_gap
        self.target, self.vix_src, self.k = target, vix_src, check_every   # target "close" = bot: close >= 2x fill

    def on_minute(self, acct, day, m, cal, dctx):
        i, n, exp = cal
        if m == ENTRY_MIN and n - 1 - i == self.dte and n >= self.dte + 1:
            vix = dctx["vix"] if self.vix_src == "prev" else dctx["vix_0920"]
            gap = day.spot[m] / dctx["prev_close"] - 1
            if (self.vix_max is None or vix <= self.vix_max) and abs(gap) > self.min_gap:
                typ = "CE" if gap > 0 else "PE"
                K = atm_strike(day.spot[m])
                p = real(day, K, typ, m)
                if p is not None:
                    lot = get_lot_size(acct.date)
                    per_lot = (p + slip(p)) * lot
                    eq = acct.equity()
                    lots = int(self.frac * eq // per_lot)
                    if lots == 0 and per_lot <= 2 * self.frac * eq:
                        lots = 1
                    if lots:
                        t = acct.open_group(day, m, self.sleeve, [(typ, K, lots * lot)],
                                            dict(exp=exp, gap=gap, vix=vix, lots=lots))
                        if t and self.tp:
                            t["meta"]["limit"] = t["legs"][0]["entry"] * (1 + self.tp)
        for t in [t for t in acct.open if t["sleeve"] == self.sleeve]:
            if t["entry"] == acct.date and m <= t["entry_min"]:
                continue
            leg = t["legs"][0]
            lim = t["meta"].get("limit")
            if lim and self.target == "close":
                if m % self.k == 0 and leg["mark"] >= lim and real(day, leg["K"], leg["typ"], m) is not None:
                    acct.close_group(t, day, m, "target")
                    continue
            elif lim:
                hi = real(day, leg["K"], leg["typ"], m, high=True)
                if hi is not None and hi >= lim + 0.05:
                    acct.close_group(t, day, m, "target", limit=lim)
                    continue
            if acct.date == t["meta"]["exp"] and m >= EXIT_MIN:
                acct.close_group(t, day, m, "expiry")


class Weekly:
    """Generic weekly structure on the nearest expiry, entered `dte` sessions before expiry at `m_in`.
    legs(spot, K_atm, S_straddle, direction) -> list of (typ, strike, +1 long / -1 short, ratio).
    direction: None (non-directional), "gap" (sign of 09:30 gap vs previous close), "bull", "bear".
    Exit: stop when loss >= stop x reference (credit for credit trades, debit for debit trades),
    target when profit >= tp x reference, spot stop when |spot - entry spot| >= spot_stop points,
    otherwise at 15:15 on expiry day (or `exit_dte` sessions before expiry at 15:15).
    Sizing: mode "fixed" = `lots` lots; "risk" = weight x equity / (capital per lot), capital per lot =
    margin (naked short: MARGIN_PCT x notional), max loss (defined risk) or premium (debit)."""

    def __init__(self, name, legs, dte=4, m_in=ENTRY_MIN, direction=None, stop=None, tp=None,
                 spot_stop=None, exit_dte=0, mode="fixed", lots=1, weight=0.1, vix_max=None, vix_min=None,
                 min_gap=0.0, check_every=1, tp_limit=False, vrp_min=None, vrp_max=None):
        self.sleeve = name
        self.legs, self.dte, self.m_in, self.dir = legs, dte, m_in, direction
        self.stop, self.tp, self.spot_stop, self.exit_dte = stop, tp, spot_stop, exit_dte
        self.mode, self.lots, self.w = mode, lots, weight
        self.vix_max, self.vix_min, self.min_gap, self.k, self.tp_limit = vix_max, vix_min, min_gap, check_every, tp_limit
        self.vrp_min, self.vrp_max = vrp_min, vrp_max

    def on_minute(self, acct, day, m, cal, dctx):
        i, n, exp = cal
        if m == self.m_in and n - 1 - i == self.dte:
            self.enter(acct, day, m, exp, dctx)
        for t in [t for t in acct.open if t["sleeve"] == self.sleeve]:
            if t["entry"] == acct.date and m <= t["entry_min"]:
                continue
            last = n - 1 - i == self.exit_dte and m >= EXIT_MIN
            if last or (acct.date == t["meta"]["exp"] and m >= EXIT_MIN):
                acct.close_group(t, day, m, "time")
                continue
            if self.tp_limit and len(t["legs"]) == 1:
                leg = t["legs"][0]
                hi = real(day, leg["K"], leg["typ"], m, high=True)
                if hi is not None and hi >= t["meta"]["limit"] + 0.05:
                    acct.close_group(t, day, m, "target", limit=t["meta"]["limit"])
                continue
            if m % self.k:
                continue
            g = acct.group_mtm(t)
            ref = t["meta"]["ref"]
            if self.stop and g <= -self.stop * ref:
                acct.close_group(t, day, m, "stop", urgent=True)
            elif self.tp and g >= self.tp * ref:
                acct.close_group(t, day, m, "target")
            elif self.spot_stop and abs(day.spot[m] - t["meta"]["spot"]) >= self.spot_stop:
                acct.close_group(t, day, m, "spotstop", urgent=True)

    def enter(self, acct, day, m, exp, dctx):
        vix = dctx["vix"]
        if (self.vix_max and vix > self.vix_max) or (self.vix_min and vix < self.vix_min):
            return
        vrp = vix - dctx.get("prev_rv20", np.nan)
        if (self.vrp_min is not None and not vrp >= self.vrp_min) or (self.vrp_max is not None and not vrp <= self.vrp_max):
            return
        S0 = day.spot[m]
        gap = S0 / dctx["prev_close"] - 1
        d = 0
        if self.dir == "gap":
            if abs(gap) <= self.min_gap:
                return
            d = 1 if gap > 0 else -1
        elif self.dir == "bull":
            d = 1
        elif self.dir == "bear":
            d = -1
        K = atm_strike(S0)
        ce, pe = real(day, K, "CE", m), real(day, K, "PE", m)
        if ce is None or pe is None:
            return
        spec = self.legs(S0, K, ce + pe, d)
        if spec is None or any(abs(k - K) > WINDOW_MAX for _, k, _, _ in spec):
            return
        px = [real(day, k, typ, m) for typ, k, _, _ in spec]
        if any(p is None for p in px):
            return
        lot = get_lot_size(acct.date)
        mpct = margin_pct(acct.date, acct.date == exp)
        net = sum(-s * r * p for (_, _, s, r), p in zip(spec, px))     # >0 credit per unit (approx, pre-slip)
        shorts = [(typ, k) for typ, k, s, _ in spec if s < 0]
        longs = [(typ, k) for typ, k, s, _ in spec if s > 0]
        if not longs:
            cap = mpct * S0 * lot
        elif not shorts:
            cap = -net * lot
        else:
            wid = 0.0
            for typ in ("CE", "PE"):
                ks = [k for t_, k, s, _ in spec if t_ == typ and s < 0]
                kl = [k for t_, k, s, _ in spec if t_ == typ and s > 0]
                if ks and kl:
                    wid = max(wid, max(abs(a - b) for a in ks for b in kl))
                elif ks:
                    wid = max(wid, mpct * S0)
            cap = max(wid - max(net, 0), 1.0) * lot
        eq = acct.equity()
        lots = self.lots if self.mode == "fixed" else int(self.w * eq // cap)
        if self.mode != "fixed":
            lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // cap))
        if lots < 1:
            return
        t = acct.open_group(day, m, self.sleeve, [(typ, k, s * r * lots * lot) for typ, k, s, r in spec],
                            dict(exp=exp, spot=S0, lots=lots, gap=round(gap, 5), vix=vix, dirn=d,
                                 margin=cap * lots))
        if t:
            t["meta"]["ref"] = abs(t["credit"]) if t["credit"] else 1.0
            if self.tp_limit:
                t["meta"]["limit"] = t["legs"][0]["entry"] * (1 + self.tp)


# ------------------------------------------------------------------ replay
_DATA = {}


def data():
    if not _DATA:
        _DATA["days"], _DATA["daily"] = load_days(), load_daily()
        v = pd.read_csv("data/raw/india_vix_daily_dhan.csv", parse_dates=["date"]).set_index("date")["close"]
        # previous session's VIX close, known before the open (2021 minute VIX is mostly missing)
        _DATA["daily"]["vix_prev"] = v.reindex(v.index.union(_DATA["daily"].index)).ffill().shift(1).reindex(_DATA["daily"].index)
    return _DATA["days"], _DATA["daily"]


def replay(strategies, start=1_000_000, running=True, d0="2021-01-01", d1="2099"):
    return replay_many([("x", strategies, start, running)], d0, d1)["x"]


def replay_many(configs, d0="2021-01-01", d1="2099"):
    """configs: list of (name, [strategies], start, running). One pass over the data for all accounts."""
    days, daily = data()
    info, tds = calendar(days, daily)
    closes = daily["close"]
    accts = [(nm, st, Account(s0, run)) for nm, st, s0, run in configs]
    eqs = {nm: [] for nm, _, _ in accts}
    last_month = None
    for d in [d for d in tds if d in info and d0 <= d <= d1]:
        day = days[d]
        loc = daily.index.get_loc(pd.Timestamp(d))
        dctx = dict(vix_0920=daily["vix_0920"].iloc[loc], prev_close=closes.iloc[loc - 1] if loc else np.nan,
                    prev_rv20=daily["rv20"].iloc[loc - 1] if loc else np.nan,
                    vix=daily["vix_prev"].iloc[loc])
        new_month = d[:7] != last_month
        last_month = d[:7]
        cal = info[d]
        for nm, st, a in accts:
            a.date = d
            if a.running and new_month:
                a.cash -= RUNNING
                a.trades.append(dict(sleeve="running", entry=d, exit=d, pnl=-RUNNING, open=False, legs=[],
                                     costs=RUNNING, credit=0, meta={}, reason="fixed"))
        for m in range(NMIN):
            for nm, st, a in accts:
                if a.legs:
                    a.update_marks(day, m)
                for s in st:
                    s.on_minute(a, day, m, cal, dctx)
        for nm, st, a in accts:
            eqs[nm].append((d, a.equity(), a.cash))
    out = {}
    for nm, st, a in accts:
        E = pd.DataFrame(eqs[nm], columns=["date", "equity", "cash"])
        T = pd.DataFrame([dict(sleeve=t["sleeve"], entry=t["entry"], exit=t["exit"], reason=t["reason"],
                               pnl=round(t["pnl"], 2), costs=round(t["costs"], 2), credit=round(t["credit"], 2),
                               **{k: v for k, v in t["meta"].items() if k not in ("exp",)})
                          for t in a.trades if not t["open"]])
        out[nm] = (E, T, a)
    return out


# ------------------------------------------------------------------ metrics
def pf(x):
    x = pd.Series(x, dtype=float)
    l = -x[x < 0].sum()
    return round(x[x > 0].sum() / l, 2) if l > 0 else float("inf")


def metrics(E, T, start):
    e = E.equity.to_numpy()
    eq = np.concatenate([[start], e])
    yrs = (pd.Timestamp(E.date.iloc[-1]) - pd.Timestamp(E.date.iloc[0])).days / 365.25
    r = pd.Series(eq).pct_change().dropna()
    tr = T[T.sleeve != "running"]
    wk = T.groupby(pd.to_datetime(T.exit).dt.to_period("W")).pnl.sum()
    return dict(cagr=round(100 * ((eq[-1] / start) ** (1 / yrs) - 1), 1),
                max_dd=round(100 * (eq / np.maximum.accumulate(eq) - 1).min(), 1),
                sharpe=round(r.mean() / r.std() * np.sqrt(252), 2),
                pf_trades=pf(tr.pnl), pf_trades_incl_running=pf(T.pnl), pf_weeks=pf(wk),
                trades=len(tr), win=round(100 * (tr.pnl > 0).mean(), 1),
                final=int(eq[-1]))


def by_year(E, start):
    out, prev = {}, start
    for y, g in E.groupby(E.date.str[:4]):
        out[y] = round(100 * (g.equity.iloc[-1] / prev - 1), 1)
        prev = g.equity.iloc[-1]
    return out


def report(name, E, T, acct, start):
    full = metrics(E, T, start)
    # IS / OOS: CAGR from the equity at the boundary
    Ei, Eo = E[E.date <= IS_END], E[E.date > IS_END]
    eq_is = np.concatenate([[start], Ei.equity.to_numpy()])
    yrs_is = (pd.Timestamp(IS_END) - pd.Timestamp(E.date.iloc[0])).days / 365.25
    eq_oos = np.concatenate([[Ei.equity.iloc[-1]], Eo.equity.to_numpy()])
    yrs_oos = (pd.Timestamp(Eo.date.iloc[-1]) - pd.Timestamp(IS_END)).days / 365.25
    dd = lambda q: round(100 * (q / np.maximum.accumulate(q) - 1).min(), 1)
    Ti, To = T[T.exit <= IS_END], T[T.exit > IS_END]
    row = dict(name=name, start=start, **full,
               is_cagr=round(100 * ((eq_is[-1] / start) ** (1 / yrs_is) - 1), 1), is_dd=dd(eq_is),
               is_pf=pf(Ti[Ti.sleeve != "running"].pnl),
               oos_cagr=round(100 * ((eq_oos[-1] / eq_oos[0]) ** (1 / yrs_oos) - 1), 1), oos_dd=dd(eq_oos),
               oos_pf=pf(To[To.sleeve != "running"].pnl),
               unobserved_fills=acct.unobserved,
               years=json.dumps(by_year(E, start)),
               pf_by_sleeve=json.dumps({s: pf(g.pnl) for s, g in T[T.sleeve != "running"].groupby("sleeve")}),
               n_by_sleeve=json.dumps(T[T.sleeve != "running"].groupby("sleeve").size().to_dict()))
    return row


def show(r):
    print(f"{r['name']:46s} start {r['start']/1e5:4.0f}L CAGR {r['cagr']:5.1f} DD {r['max_dd']:6.1f} "
          f"PF {r['pf_trades']:.2f}/{r['pf_trades_incl_running']:.2f} wkPF {r['pf_weeks']:.2f} Sh {r['sharpe']:.2f} "
          f"n {r['trades']} | IS {r['is_cagr']:5.1f}/{r['is_dd']:6.1f}/{r['is_pf']:.2f} "
          f"OOS {r['oos_cagr']:5.1f}/{r['oos_dd']:6.1f}/{r['oos_pf']:.2f} | unobs {r['unobserved_fills']} "
          f"| {r['pf_by_sleeve']} | {r['years']}", flush=True)


if __name__ == "__main__":
    rows = []
    start = 1_000_000
    for name, mk in [
        ("L2 30%", lambda: [StaggeredStrangle(0.30)]),
        ("GAP 3% vix<=15", lambda: [GapBuy(0.03)]),
        ("L2 30% + GAP 3%", lambda: [StaggeredStrangle(0.30), GapBuy(0.03)]),
    ]:
        E, T, acct = replay(mk(), start)
        r = report(name, E, T, acct, start)
        show(r)
        rows.append(r)
    pd.DataFrame(rows).to_csv("data/reports/realtrade_summary.csv", index=False)
