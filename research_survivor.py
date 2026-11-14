"""
Survivor (Raahi Bhushan's open-source trend-following option seller) on the real-market replay.

Original rules (C:/Users/Admin/Documents/trading-algo/strategy/survivor.py):
  * Two reference levels, PE and CE, start at spot.
  * Spot more than `gap` above the PE reference -> sell PE, qty = clip x int(move / gap), strike nearest
    spot - `sgap`; if its premium < `min_prem`, step one strike closer. PE reference += gap x mult.
  * Mirror for CE on falls.
  * Reset: after a PE sale, if spot falls more than `reset` below the PE reference, the reference is
    pulled to spot + reset (mirror for CE).
  * The repo code has NO exit. Exits tested here come from the author's articles and standard rules.

Replay assumptions (same simulator as Strategy 6, research_realtrade.py):
  * Nearest weekly expiry only, real traded prices only, strikes within +/-450 of ATM.
  * Spot is the 1-minute close; references reset at 09:20 on each trading session (the script keeps
    state in memory and is restarted daily). New sales 09:21-15:00 only.
  * Clip size = `clip_w` x equity of naked-short margin (11.6% of notional, Dhan level, + 2% ELM on
    expiry day), whole lots, total margin capped at 90% of equity. Compounding.
  * Costs: statutory per order, slippage on every fill (urgent on stops), Rs 1,990/month running costs.
  * Parameters are chosen on 2021-2023; 2024+ is out-of-sample.

Wave (grid around the Nifty future) is tested separately in `wave_backtest()` on 1-minute Nifty SPOT
OHLC as a proxy for the near future (no futures data in the project; basis ignored).

Writes data/reports/survivor_grid.csv, survivor_best_trades.csv, survivor_best_equity.csv,
survivor_wave.csv
"""
import json
import sys
import time

import numpy as np
import pandas as pd

import research_realtrade as rr
from research_realtrade import Account, real, replay_many, report, show, ENTRY_MIN, EXIT_MIN, MAX_UTIL
from cost_model import get_lot_size, future_fill_cost, FUT_SLIP_PTS, MONTHLY_RUNNING_COSTS
from engine import atm_strike, load_spot_minutes

def _daily_from_cache(days):
    """daily_features without pyarrow: same expiry rule as build_research_data.detect_expiries, from the
    engine cache (ATM straddle at 09:20 and at the close). Mock Saturday sessions have no options data
    and are absent from the cache, so they never enter."""
    rows = []
    for d in sorted(days):
        x = days[d]
        def strad(m):
            K = atm_strike(x.spot[m])
            j = x.sidx.get(float(K))
            if j is None:
                return np.nan
            return float(x.ce[m, j] + x.pe[m, j])
        last = 375
        while last > 5 and np.isnan(strad(last)):
            last -= 1
        rows.append(dict(date=d, close=x.spot[375], straddle_open=strad(5), straddle_close=strad(last)))
    s = pd.DataFrame(rows).set_index("date")
    s["next_open"] = s["straddle_open"].shift(-1)
    s["is_expiry"] = (s["next_open"] > 1.8 * s["straddle_close"]) & (
        (s["straddle_close"] < 0.65 * s["straddle_open"]) | (s["next_open"] > 1.3 * s["straddle_open"]))
    s.loc[s.index[-1], "is_expiry"] = False
    lastexp = None
    for d in s.index[s["is_expiry"]]:
        if lastexp is not None and (pd.Timestamp(d) - pd.Timestamp(lastexp)).days <= 3:
            s.loc[d, "is_expiry"] = False
        else:
            lastexp = d
    s.index = pd.to_datetime(s.index)
    r = np.log(s["close"]).diff()
    s["rv20"] = r.rolling(20).std() * np.sqrt(252) * 100
    s["vix_0920"] = np.nan
    return s


def _patch_data():
    try:
        import pyarrow  # noqa: F401
        return
    except ImportError:
        pass
    from engine import load_days
    days = load_days()
    daily = _daily_from_cache(days)
    v = pd.read_csv("data/raw/india_vix_daily_dhan.csv", parse_dates=["date"]).set_index("date")["close"]
    daily["vix_prev"] = v.reindex(v.index.union(daily.index)).ffill().shift(1).reindex(daily.index)
    rr._DATA["days"], rr._DATA["daily"] = days, daily
    print("daily built from cache:", len(daily), "sessions,", int(daily.is_expiry.sum()), "expiries,",
          daily[daily.is_expiry].index.day_name().value_counts().to_dict(), flush=True)


REAL_MARGIN = 0.116         # Dhan naked short margin, fraction of notional (2026-10-23 measurement)
REF_MIN = 5                  # 09:20
LAST_ENTRY = 345             # 15:00
WINDOW = 450


def mpct(date, expiry_day):
    return REAL_MARGIN + (0.02 if expiry_day and date >= "2024-11-20" else 0.0)


class Survivor:
    """
    gap, sgap, reset, min_prem, max_mult : original Survivor parameters
    clip_w     : margin fraction of equity per clip (one gap step)
    entry_dte  : sessions-before-expiry on which new sales are allowed (0 = expiry day)
    stop       : per-leg stop when loss >= stop x credit (0.5 = option price up 50%), None = no stop
    tp         : per-leg profit target as fraction of credit, None = none
    cheap_tp   : author's rule: close when price < cheap_tp[0] and profit > cheap_tp[1] x credit
    spot_stop  : close a leg when spot retraces this many points past the spot at which it was sold
    book_stop  : close all Survivor legs and stop for the day when open loss >= book_stop x equity
    intraday   : close everything at 15:15 every day
    hedge      : buy a long option this many points further OTM with each clip (spread), None = naked
    max_open   : cap on open clips per side
    """

    def __init__(self, name, gap=20, sgap=200, reset=30, min_prem=15, max_mult=5, clip_w=0.05,
                 entry_dte=(1, 2, 3, 4, 5, 6), stop=None, tp=None, cheap_tp=None, spot_stop=None,
                 book_stop=None, intraday=False, hedge=None, max_open=None, check_every=1, mode="trend",
                 times=(45,)):
        self.mode, self.times = mode, set(times)
        self.sleeve = name
        self.gap, self.sgap, self.reset, self.min_prem, self.max_mult = gap, sgap, reset, min_prem, max_mult
        self.clip_w, self.entry_dte = clip_w, set(entry_dte)
        self.stop, self.tp, self.cheap_tp, self.spot_stop = stop, tp, cheap_tp, spot_stop
        self.book_stop, self.intraday, self.hedge, self.max_open, self.k = book_stop, intraday, hedge, max_open, check_every
        self.ref = {"PE": None, "CE": None}
        self.flag = {"PE": 0, "CE": 0}
        self.halted = False
        self.day_eq = None

    # ----------------------------------------------------------- entries
    def _strike(self, acct, day, m, typ, s):
        K = atm_strike(s - self.sgap if typ == "PE" else s + self.sgap)
        atm = atm_strike(s)
        step = 50 if typ == "PE" else -50                 # step towards ATM
        for _ in range(20):
            if abs(K - atm) > WINDOW:
                K += step
                continue
            p = real(day, K, typ, m)
            if p is not None and p >= self.min_prem:
                return K, p
            if (typ == "PE" and K >= atm) or (typ == "CE" and K <= atm):
                return None
            K += step
        return None

    def _sell(self, acct, day, m, typ, s, mult, exp):
        if self.max_open is not None:
            n_open = sum(1 for t in acct.open if t["sleeve"] == self.sleeve and t["meta"]["typ"] == typ)
            if n_open >= self.max_open:
                return
        found = self._strike(acct, day, m, typ, s)
        if found is None:
            return
        K, p = found
        lot = get_lot_size(acct.date)
        specs = [(typ, K, -1)]
        if self.hedge:
            KH = K - self.hedge if typ == "PE" else K + self.hedge
            if abs(KH - atm_strike(s)) > 500 or real(day, KH, typ, m) is None:
                return
            ph = real(day, KH, typ, m)
            cap = max(self.hedge - (p - ph), 1.0) * lot
            specs.append((typ, KH, +1))
        naked = mpct(acct.date, acct.date == exp) * s * lot
        if not self.hedge:
            cap = naked
        eq = acct.equity()
        # lots sized on NAKED margin in both cases, so a hedged clip has the same lot count as a naked one
        lots = max(1, int(self.clip_w * eq // naked)) * mult
        lots = min(lots, int((MAX_UTIL * eq - acct.margin_used()) // naked))
        if lots < 1:
            return
        q = lots * lot
        t = acct.open_group(day, m, self.sleeve, [(ty, k, sgn * q) for ty, k, sgn in specs],
                            dict(exp=exp, typ=typ, K=K, spot=s, lots=lots, mult=mult, margin=cap * lots))
        if t is not None:
            t["meta"]["ref"] = abs(t["credit"]) if t["credit"] else 1.0
            self.flag[typ] = 1

    # ----------------------------------------------------------- per minute
    def on_minute(self, acct, day, m, cal, dctx):
        i, n, exp = cal
        dte = n - 1 - i
        s = day.spot[m]
        if m == 0:
            self.halted = False
            self.day_eq = acct.equity()
        if m == REF_MIN:
            self.ref = {"PE": s, "CE": s}
            self.flag = {"PE": 0, "CE": 0}

        mine = [t for t in acct.open if t["sleeve"] == self.sleeve]

        # ---- exits
        if mine:
            if (acct.date == exp or self.intraday) and m >= EXIT_MIN:
                for t in mine:
                    acct.close_group(t, day, m, "expiry" if acct.date == exp else "eod")
                mine = []
            elif self.book_stop and m % self.k == 0:
                tot = sum(acct.group_mtm(t) for t in mine)
                if tot <= -self.book_stop * self.day_eq:
                    for t in mine:
                        acct.close_group(t, day, m, "bookstop", urgent=True)
                    mine = []
                    self.halted = True
            for t in mine:
                if t["entry"] == acct.date and m <= t["entry_min"]:
                    continue
                if m % self.k:
                    continue
                g = acct.group_mtm(t)
                ref = t["meta"]["ref"]
                short = t["legs"][0]
                if self.stop is not None and g <= -self.stop * ref:
                    acct.close_group(t, day, m, "stop", urgent=True)
                elif self.tp is not None and g >= self.tp * ref:
                    acct.close_group(t, day, m, "target")
                elif self.cheap_tp and short["mark"] < self.cheap_tp[0] and g >= self.cheap_tp[1] * ref:
                    acct.close_group(t, day, m, "cheap")
                elif self.spot_stop is not None and (
                        (t["meta"]["typ"] == "PE" and s <= t["meta"]["spot"] - self.spot_stop) or
                        (t["meta"]["typ"] == "CE" and s >= t["meta"]["spot"] + self.spot_stop)):
                    acct.close_group(t, day, m, "spotstop", urgent=True)

        # ---- entries (original Survivor logic)
        if self.halted or dte not in self.entry_dte or not (REF_MIN < m <= LAST_ENTRY) or self.ref["PE"] is None:
            return
        if self.mode == "time":                       # baseline: same strikes/sizing, sold on a clock
            if m in self.times:
                self._sell(acct, day, m, "PE", s, 1, exp)
                self._sell(acct, day, m, "CE", s, 1, exp)
            return
        if self.mode == "contra":                     # mirror trigger: sell CE on rises, PE on falls
            if s > self.ref["PE"] and round(s - self.ref["PE"]) > self.gap:
                mult = int(round(s - self.ref["PE"]) / self.gap)
                if mult <= self.max_mult:
                    self.ref["PE"] += self.gap * mult
                    self._sell(acct, day, m, "CE", s, mult, exp)
            if s < self.ref["CE"] and round(self.ref["CE"] - s) > self.gap:
                mult = int(round(self.ref["CE"] - s) / self.gap)
                if mult <= self.max_mult:
                    self.ref["CE"] -= self.gap * mult
                    self._sell(acct, day, m, "PE", s, mult, exp)
            return
        if s > self.ref["PE"]:
            diff = round(s - self.ref["PE"])
            if diff > self.gap:
                mult = int(diff / self.gap)
                if mult <= self.max_mult:
                    self.ref["PE"] += self.gap * mult
                    self._sell(acct, day, m, "PE", s, mult, exp)
        if s < self.ref["CE"]:
            diff = round(self.ref["CE"] - s)
            if diff > self.gap:
                mult = int(diff / self.gap)
                if mult <= self.max_mult:
                    self.ref["CE"] -= self.gap * mult
                    self._sell(acct, day, m, "CE", s, mult, exp)
        if self.flag["PE"] and self.ref["PE"] - s > self.reset:
            self.ref["PE"] = s + self.reset
        if self.flag["CE"] and s - self.ref["CE"] > self.reset:
            self.ref["CE"] = s - self.reset


# ---------------------------------------------------------------- day-level stats (Sensibull "accuracy")
def day_stats(E, start):
    eq = np.concatenate([[start], E.equity.to_numpy()])
    d = np.diff(eq)
    active = d[np.abs(d) > 1e-6]
    return dict(day_win=round(100 * (active > 0).mean(), 1) if len(active) else 0.0,
                worst_day=round(100 * (np.diff(eq) / eq[:-1]).min(), 1))


# ---------------------------------------------------------------- Wave on spot-as-future
def wave_backtest(gap=25, lots=1, scale=True, inv_cap=None, d0="2021-01-01", d1="2099", start=1_000_000,
                  cool_min=0):
    """Buy limit at anchor-gap_b, sell limit at anchor+gap_s; a fill moves the anchor to the fill price and
    re-arms both orders (cool_min minutes later). With scale, the gap on the side that adds to inventory
    widens by the author's table (1.3, 1.7, 2.5, 3, 10, ...). inv_cap = max |inventory| in lots.
    Marked at each session close; positions carried overnight (NRML). Nifty spot OHLC as the future."""
    try:
        mins = load_spot_minutes()
    except ImportError:                    # no pyarrow: 1-min closes from the engine cache (fills on close)
        days = rr._DATA["days"]
        mins = {d: np.repeat(x.spot[:, None], 4, axis=1) for d, x in days.items()}
    table = [1.0, 1.3, 1.7, 2.5, 3, 10, 10, 10, 15, 15, 15]
    cash, inv, anchor = float(start), 0, None
    eqs, fills, last_month, wait = [], 0, None, 0
    for d in sorted(k for k in mins if d0 <= k <= d1):
        a = mins[d]
        lot = get_lot_size(d)
        if d[:7] != last_month:
            cash -= sum(MONTHLY_RUNNING_COSTS.values())
            last_month = d[:7]
        if anchor is None:
            anchor = a[0, 3]
        for m in range(len(a)):
            o, h, l, c = a[m]
            if np.isnan(c):
                continue
            if wait:
                wait -= 1
                continue
            k = abs(inv) // 1 if scale else 0
            mb = table[min(k, 10)] if (scale and inv > 0) else 1.0      # long -> harder to buy more
            ms = table[min(k, 10)] if (scale and inv < 0) else 1.0      # short -> harder to sell more
            buy_px, sell_px = anchor - gap * mb, anchor + gap * ms
            can_buy = inv_cap is None or inv < inv_cap
            can_sell = inv_cap is None or inv > -inv_cap
            if can_sell and h >= sell_px:
                q = lots * lot
                cash += sell_px * q - future_fill_cost(sell_px, q, False, d)
                inv -= lots
                anchor, fills, wait = sell_px, fills + 1, cool_min
            elif can_buy and l <= buy_px:
                q = lots * lot
                cash -= buy_px * q + future_fill_cost(buy_px, q, True, d)
                inv += lots
                anchor, fills, wait = buy_px, fills + 1, cool_min
        last = a[~np.isnan(a[:, 3]), 3][-1]
        eqs.append((d, cash + inv * lot * last, inv))
    E = pd.DataFrame(eqs, columns=["date", "equity", "inv"])
    e = np.concatenate([[start], E.equity.to_numpy()])
    yrs = (pd.Timestamp(E.date.iloc[-1]) - pd.Timestamp(E.date.iloc[0])).days / 365.25
    ret = (e[-1] / start) ** (1 / yrs) - 1 if e[-1] > 0 else -1.0
    Ei = E[E.date <= rr.IS_END]
    return dict(gap=gap, lots=lots, scale=scale, inv_cap=inv_cap, fills=fills,
                cagr=round(100 * ret, 1), max_dd=round(100 * (e / np.maximum.accumulate(e) - 1).min(), 1),
                pnl_is=int(Ei.equity.iloc[-1] - start), pnl_oos=int(E.equity.iloc[-1] - Ei.equity.iloc[-1]),
                max_inv=int(E.inv.abs().max()), final_inv=int(E.inv.iloc[-1]), **day_stats(E, start))


# ---------------------------------------------------------------- grids
def configs(stage):
    """Returns list of (name, factory)."""
    out = []
    if stage == "base":
        # 1) the repo code as written (no exit, hold to expiry), repo defaults and the author's article values
        for gap in (15, 20, 30, 50):
            for sgap in (200, 300):
                for reset in (30, 90):
                    nm = f"raw g{gap} s{sgap} r{reset}"
                    out.append((nm, lambda gap=gap, sgap=sgap, reset=reset, nm=nm:
                                Survivor(nm, gap=gap, sgap=sgap, reset=reset, clip_w=0.04,
                                         entry_dte=(0, 1, 2, 3, 4, 5, 6))))
    elif stage == "baseline":
        allday = (0, 1, 2, 3, 4, 5, 6)
        for sgap in (200, 300):
            for times, tn in (((45,), "1x 10:00"), ((45, 165), "2x 10:00+12:00"), ((45, 165, 285), "3x")):
                nm = f"time {tn} s{sgap}"
                out.append((nm, lambda nm=nm, sgap=sgap, times=times:
                            Survivor(nm, sgap=sgap, clip_w=0.04, mode="time", times=times, entry_dte=allday)))
            for gap, reset in ((50, 90), (20, 90), (30, 30)):
                nm = f"contra g{gap} s{sgap} r{reset}"
                out.append((nm, lambda nm=nm, gap=gap, sgap=sgap, reset=reset:
                            Survivor(nm, gap=gap, sgap=sgap, reset=reset, clip_w=0.04, mode="contra",
                                     entry_dte=allday)))
    elif stage == "combo":
        # 3) spot stop x open-clip cap x clip size; selection on 2021-23 only
        allday = (0, 1, 2, 3, 4, 5, 6)
        for gap, sgap, reset in ((30, 200, 30), (50, 200, 90), (30, 200, 90)):
            for ss in (75, 100, 125):
                for mo in (None, 3, 4, 6):
                    for cw in (0.04, 0.06):
                        nm = f"g{gap} s{sgap} r{reset} spot{ss} max{mo} w{int(cw*100)}"
                        kw = dict(gap=gap, sgap=sgap, reset=reset, clip_w=cw, spot_stop=ss, max_open=mo,
                                  entry_dte=allday)
                        out.append((nm, lambda nm=nm, kw=kw: Survivor(nm, **kw)))
    elif stage == "exits":
        # 2) exits and adjustments on the two most common settings
        for gap, sgap, reset in ((30, 200, 30), (20, 300, 90), (50, 200, 90)):
            base = dict(gap=gap, sgap=sgap, reset=reset, clip_w=0.04)
            tag = f"g{gap} s{sgap} r{reset}"
            variants = {
                "hold": {},
                "stop50": dict(stop=0.5),
                "stop100": dict(stop=1.0),
                "stop50+cheap": dict(stop=0.5, cheap_tp=(20, 0.5)),
                "stop30/50(author)": dict(stop=0.3, cheap_tp=(20, 0.5)),
                "tp50": dict(tp=0.5),
                "stop100+tp70": dict(stop=1.0, tp=0.7),
                "spot100": dict(spot_stop=100),
                "spot150": dict(spot_stop=150),
                "book2%": dict(book_stop=0.02),
                "intraday": dict(intraday=True),
                "intraday+stop50": dict(intraday=True, stop=0.5),
                "hedge200": dict(hedge=200),
                "hedge200+stop100": dict(hedge=200, stop=1.0),
                "noexpday": dict(entry_dte=(1, 2, 3, 4, 5, 6)),
                "expday-only": dict(entry_dte=(0,)),
                "maxopen4": dict(max_open=4),
                "maxopen4+stop100": dict(max_open=4, stop=1.0),
            }
            for vn, kw in variants.items():
                nm = f"{tag} {vn}"
                kw2 = {**base, **kw}
                if vn not in ("noexpday", "expday-only"):
                    kw2.setdefault("entry_dte", (0, 1, 2, 3, 4, 5, 6))
                out.append((nm, lambda nm=nm, kw2=kw2: Survivor(nm, **kw2)))
    return out


def run(stage, start=1_000_000, workers=1, part=None):
    cfgs = configs(stage)
    if part is not None:
        k, n = part
        cfgs = cfgs[k::n]
    rows = []
    t0 = time.time()
    res = replay_many([(nm, [f()], start, True) for nm, f in cfgs])
    for nm, _ in cfgs:
        E, T, a = res[nm]
        if len(T[T.sleeve != "running"]) == 0:
            continue
        r = report(nm, E, T, a, start)
        r.update(day_stats(E, start))
        tr = T[T.sleeve != "running"]
        r["reasons"] = json.dumps(tr.reason.value_counts().to_dict())
        r["avg_lots"] = round(tr["lots"].mean(), 1) if "lots" in tr else None
        rows.append(r)
        show(r)
        print(f"    day_win {r['day_win']}% worst_day {r['worst_day']}% reasons {r['reasons']}", flush=True)
    print(f"{stage}: {len(cfgs)} configs in {time.time() - t0:.0f}s", flush=True)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    _patch_data()
    stage = sys.argv[1] if len(sys.argv) > 1 else "base"
    part = tuple(int(x) for x in sys.argv[2].split("/")) if len(sys.argv) > 2 else None
    if stage == "wave":
        rows = []
        for gap in (15, 25, 50, 100):
            for scale in (False, True):
                for cap in (None, 3, 10):
                    r = wave_backtest(gap=gap, lots=1, scale=scale, inv_cap=cap)
                    print(r, flush=True)
                    rows.append(r)
        pd.DataFrame(rows).to_csv("data/reports/survivor_wave.csv", index=False)
    elif stage == "best":
        name = sys.argv[2]
        kw = json.loads(sys.argv[3])
        out = replay_many([(name, [Survivor(name, **kw)], 1_000_000, True)])
        E, T, a = out[name]
        r = report(name, E, T, a, 1_000_000)
        r.update(day_stats(E, 1_000_000))
        show(r)
        print(day_stats(E, 1_000_000))
        T.to_csv("data/reports/survivor_best_trades.csv", index=False)
        E.to_csv("data/reports/survivor_best_equity.csv", index=False)
    else:
        df = run(stage, part=part)
        suffix = f"_{part[0]}of{part[1]}" if part else ""
        df.to_csv(f"data/reports/survivor_{stage}{suffix}.csv", index=False)
