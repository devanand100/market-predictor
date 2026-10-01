"""
Family 5: Institutional short-vol structures on NIFTY weeklies (contract-exact, 1-min data).

All strategies: enter 09:30 on the first session after expiry, exit 15:15 on expiry day.
Position checks every CHECK_EVERY minutes on 1-min closes. Size: 10 lots (650 units) per full
position so a 1-lot (65) futures hedge is fine-grained.

  S0   baseline strangle (Strategy 1 rules): +/-1.0 x straddle, stop 1.5 x credit
  S0b  short ATM straddle, no stop, no hedge
  S0c  short +/-1.0S strangle, no stop, no hedge
  L1   multi-level strangle ladder: 1/3 at +/-0.5S, 1/3 at +/-0.75S, 1/3 at +/-1.0S, stop 1.5 x credit
  L2   time-staggered strangle: 1/3 entered on each of the first 3 sessions, each tranche stop 1.5x
  D1   short straddle, delta-hedged with futures when |net delta| > band
  D2   short +/-1.0S strangle, delta-hedged with futures
  D3   short +/-1.0S strangle, delta-neutral by rolling the untested leg when one leg >= 2x the other,
       max 3 adjustments, hard stop at 2 x initial credit
  G1   LONG ATM straddle, delta-hedged (gamma scalping)
Delta: Black-Scholes with the Dhan IV of that strike (ATM IV if missing), calendar time to 15:30 expiry.
Returns are measured on a margin-based capital (9% of notional per strangle pair / 60% utilisation),
the same rule as Strategy 1, so all rows are comparable. Fixed running costs excluded here.
"""
import math
import numpy as np
import pandas as pd

import research_positional as rp
from engine import load_days, load_daily, atm_strike
import cost_model
from cost_model import option_fill_cost, future_fill_cost, slip

LOT = 65
CHECK_EVERY = 5
EXIT_MIN = 360
ENTRY_MIN = 15
WINDOW_MAX = 450          # keep strikes inside the stored +/-500 window
MARGIN_PCT, MAX_USE = 0.09, 0.60
BASIS_PTS_PER_DAY = 0.0   # stress: futures basis drift charged against |hedge| each session
ATM_IV_ONLY = False       # stress: ignore per-strike IV, use ATM IV for every delta


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_delta(S, K, iv_pct, T, typ):
    if T <= 0 or not iv_pct or iv_pct != iv_pct or iv_pct <= 0.5:
        return (1.0 if S > K else 0.0) if typ == "CE" else (-1.0 if S < K else 0.0)
    s = iv_pct / 100
    d1 = (math.log(S / K) + 0.5 * s * s * T) / (s * math.sqrt(T))
    return ncdf(d1) if typ == "CE" else ncdf(d1) - 1


class Book:
    """Option legs + futures hedge with full cost accounting (Rs)."""

    def __init__(self, date):
        self.legs = []          # dicts: typ, K, qty (+long/-short units), entry
        self.fut = 0            # futures units (+long)
        self.fut_px = 0.0       # average price of futures position
        self.realized = 0.0
        self.costs = 0.0
        self.date = date
        self.n_adj = 0
        self.n_hedges = 0

    def open(self, day, typ, K, qty, m):
        p = day.px(K, typ, m)
        fill = p + slip(p) if qty > 0 else p - slip(p)
        self.costs += option_fill_cost(fill, abs(qty), qty > 0, self.date)
        self.legs.append({"typ": typ, "K": K, "qty": qty, "entry": fill})
        return fill

    def close(self, day, leg, m, urgent=False):
        p = day.px(leg["K"], leg["typ"], m)
        fill = p - slip(p, urgent) if leg["qty"] > 0 else p + slip(p, urgent)
        fill = max(0.05, fill)
        self.costs += option_fill_cost(fill, abs(leg["qty"]), leg["qty"] < 0, self.date)
        self.realized += leg["qty"] * (fill - leg["entry"])
        self.legs.remove(leg)

    def close_all(self, day, m, urgent=False):
        for leg in list(self.legs):
            self.close(day, leg, m, urgent)
        if self.fut:
            self.trade_fut(day.spot[m], -self.fut)

    def trade_fut(self, px, units):
        if units == 0:
            return
        fill = px + cost_model.FUT_SLIP_PTS if units > 0 else px - cost_model.FUT_SLIP_PTS
        self.costs += future_fill_cost(fill, abs(units), units > 0, self.date)
        new = self.fut + units
        if self.fut == 0 or np.sign(self.fut) == np.sign(units):       # adding
            self.fut_px = (self.fut_px * self.fut + fill * units) / new
        else:                                                           # reducing / flipping
            closed = min(abs(units), abs(self.fut)) * np.sign(self.fut)
            self.realized += closed * (fill - self.fut_px)
            if abs(units) > abs(self.fut):
                self.fut_px = fill
        self.fut = new
        if new == 0:
            self.fut_px = 0.0
        self.n_hedges += 1

    def mtm(self, day, m):
        v = sum(l["qty"] * (day.px(l["K"], l["typ"], m) - l["entry"]) for l in self.legs)
        if self.fut:
            v += self.fut * (day.spot[m] - self.fut_px)
        return self.realized + v - self.costs

    def delta(self, day, m, T):
        S = day.spot[m]
        K_atm = atm_strike(S)
        j = day.col(K_atm)
        atm_iv = np.nan
        if j is not None:
            atm_iv = np.nanmean([day.ce_iv[m, j], day.pe_iv[m, j]])
        tot = self.fut
        for l in self.legs:
            jj = None if ATM_IV_ONLY else day.col(l["K"])
            iv = np.nan
            if jj is not None:
                iv = (day.ce_iv if l["typ"] == "CE" else day.pe_iv)[m, jj]
            if not (iv == iv) or iv <= 0.5:
                iv = atm_iv if atm_iv == atm_iv else 15.0
            tot += l["qty"] * bs_delta(S, l["K"], float(iv), T, l["typ"])
        return tot


def minutes_path(window, start_day_idx=0):
    for i, d in enumerate(window):
        if i < start_day_idx:
            continue
        a = ENTRY_MIN if i == start_day_idx else 0
        b = EXIT_MIN if i == len(window) - 1 else 375
        for m in range(a, b + 1, CHECK_EVERY):
            yield i, d, m
    # guarantee the exact exit minute
    yield len(window) - 1, window[-1], EXIT_MIN


def t_years(date, m, expiry):
    now = pd.Timestamp(date) + pd.Timedelta(minutes=555 + m)
    end = pd.Timestamp(expiry) + pd.Timedelta(hours=15, minutes=30)
    return max((end - now).total_seconds(), 600) / (365 * 24 * 3600)


def straddle_S(day, m):
    K = atm_strike(day.spot[m])
    return K, day.px(K, "CE", m) + day.px(K, "PE", m)


def width(S, mult):
    return min(WINDOW_MAX, max(50, int(round(mult * S / 50)) * 50))


def run(days, window, kind, lots=10, band_lots=None, stop_mult=None):
    exp = window[-1]
    d0 = days[window[0]]
    spot0 = d0.spot[ENTRY_MIN]
    K, S = straddle_S(d0, ENTRY_MIN)
    book = Book(window[0])
    q = lots * LOT
    tranches = []   # for L2: (legs added, credit, stopped)

    def sell_strangle(day, m, mult, qty):
        Kx, Sx = straddle_S(day, m)
        w = 0 if mult == 0 else width(Sx, mult)
        c = book.open(day, "CE", Kx + w, -qty, m)
        p = book.open(day, "PE", Kx - w, -qty, m)
        return c + p

    if kind in ("S0", "S0c", "D2", "D3"):
        credit = sell_strangle(d0, ENTRY_MIN, 1.0, q) * q
    elif kind in ("S0b", "D1"):
        credit = sell_strangle(d0, ENTRY_MIN, 0, q) * q
    elif kind == "L1":
        per = q // 3
        credit = sum(sell_strangle(d0, ENTRY_MIN, mlt, per) for mlt in (0.5, 0.75, 1.0)) * per
    elif kind == "L2":
        per = (q // 3)
        credit = sell_strangle(d0, ENTRY_MIN, 1.0, per) * per
        tranches.append({"legs": list(book.legs), "credit": credit, "open": True})
    elif kind == "G1":
        Kx = atm_strike(spot0)
        book.open(d0, "CE", Kx, q, ENTRY_MIN)
        book.open(d0, "PE", Kx, q, ENTRY_MIN)
        credit = 0.0
    if any(l["entry"] < 1.0 for l in book.legs if l["qty"] < 0):
        return None
    init_credit = credit
    done = False
    stopped = False
    prev_i = 0
    for i, date, m in minutes_path(window):
        day = days[date]
        if i != prev_i:
            book.costs += abs(book.fut) * BASIS_PTS_PER_DAY
            prev_i = i
        if done:
            break
        # L2: add tranches at 09:30 on days 1 and 2
        if kind == "L2" and m == ENTRY_MIN and 0 < i <= 2 and i < len(window) - 1:
            before = len(book.legs)
            c = sell_strangle(day, m, 1.0, q // 3) * (q // 3)
            tranches.append({"legs": book.legs[before:], "credit": c, "open": True})
            init_credit += c
        last = (date == exp and m >= EXIT_MIN)
        if last:
            book.close_all(day, m)
            break
        pnl = book.mtm(day, m)
        # stops
        if kind in ("S0", "L1") and stop_mult and pnl <= -stop_mult * init_credit:
            book.close_all(day, m, urgent=True)
            stopped = True
            break
        if kind == "L2":
            for t in tranches:
                if t["open"]:
                    tp = sum(l["qty"] * (day.px(l["K"], l["typ"], m) - l["entry"]) for l in t["legs"])
                    if tp <= -1.5 * t["credit"]:
                        for l in t["legs"]:
                            if l in book.legs:
                                book.close(day, l, m, urgent=True)
                        t["open"] = False
            if not book.legs and len(tranches) >= min(3, len(window) - 1):
                break
        if kind == "D3":
            if pnl <= -2.0 * init_credit:
                book.close_all(day, m, urgent=True)
                stopped = True
                break
            shorts = [l for l in book.legs if l["qty"] < 0]
            if len(shorts) == 2 and book.n_adj < 3:
                pc = {l["typ"]: day.px(l["K"], l["typ"], m) for l in shorts}
                hi_t = max(pc, key=pc.get)
                lo_t = min(pc, key=pc.get)
                if pc[lo_t] > 0.5 and pc[hi_t] >= 2.0 * pc[lo_t]:
                    untested = [l for l in shorts if l["typ"] == lo_t][0]
                    target = pc[hi_t]
                    Kat = atm_strike(day.spot[m])
                    # candidate strikes between ATM and the old untested strike
                    if lo_t == "PE":
                        cands = range(int(untested["K"]) + 50, int(Kat) + 1, 50)
                    else:
                        cands = range(int(untested["K"]) - 50, int(Kat) - 1, -50)
                    best = None
                    for k in cands:
                        v = day.px(k, lo_t, m)
                        if best is None or abs(v - target) < abs(best[1] - target):
                            best = (k, v)
                    if best is not None:
                        qty = untested["qty"]
                        book.close(day, untested, m)
                        book.open(day, lo_t, best[0], qty, m)
                        book.n_adj += 1
        if kind in ("D1", "D2", "G1") and band_lots is not None:
            T = t_years(date, m, exp)
            net = book.delta(day, m, T)
            if abs(net) > band_lots * LOT:
                n = int(round(-net / LOT)) * LOT
                book.trade_fut(day.spot[m], n)
    else:
        pass
    if book.legs or book.fut:
        book.close_all(days[exp], EXIT_MIN)
    pnl = book.realized - book.costs
    capital = MARGIN_PCT * spot0 * q / MAX_USE
    return {"entry": window[0], "exit": exp, "pnl": pnl, "ret_pct": pnl / capital * 100, "credit_rs": init_credit,
            "costs": book.costs, "stopped": stopped, "adj": book.n_adj, "hedges": book.n_hedges, "spot": spot0}


def summarize(T, name):
    out = {"strategy": name}
    for lab, sub in [("ALL", T), ("IS 21-23", T[T.entry < "2024-01-01"]), ("OOS 24-26", T[T.entry >= "2024-01-01"])]:
        r = sub.ret_pct / 100
        eq = (1 + r).cumprod()
        yrs = max((pd.Timestamp(sub.exit.iloc[-1]) - pd.Timestamp(sub.entry.iloc[0])).days / 365.25, 0.1)
        out[f"{lab} CAGR"] = round((eq.iloc[-1] ** (1 / yrs) - 1) * 100, 1)
        out[f"{lab} DD"] = round(((eq / eq.cummax()) - 1).min() * 100, 1)
        out[f"{lab} SR"] = round(r.mean() / r.std() * np.sqrt(52), 2)
    out["worst wk%"] = round(T.ret_pct.min(), 1)
    out["win%"] = round((T.pnl > 0).mean() * 100, 1)
    out["cost/credit%"] = round(T.costs.sum() / max(T.credit_rs.abs().sum(), 1) * 100, 1)
    out["hedges/wk"] = round(T.hedges.mean(), 1)
    out["adj/wk"] = round(T.adj.mean(), 2)
    return out


if __name__ == "__main__":
    import sys
    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 30)
    days, daily = load_days(), load_daily()
    cyc = rp.cycles(daily, days)
    variants = [("S0 strangle +stop (Strategy 1)", "S0", None, 1.5),
                ("S0b short straddle, naked", "S0b", None, None),
                ("S0c short strangle, naked", "S0c", None, None),
                ("L1 3-level strangle ladder", "L1", None, 1.5),
                ("L2 time-staggered strangle", "L2", None, None),
                ("D1 straddle, delta-hedged band 1 lot", "D1", 1, None),
                ("D1 straddle, delta-hedged band 2 lots", "D1", 2, None),
                ("D1 straddle, delta-hedged band 4 lots", "D1", 4, None),
                ("D2 strangle, delta-hedged band 1 lot", "D2", 1, None),
                ("D2 strangle, delta-hedged band 2 lots", "D2", 2, None),
                ("D2 strangle, delta-hedged band 4 lots", "D2", 4, None),
                ("D3 strangle, roll untested leg", "D3", None, None),
                ("G1 long straddle, gamma scalp band 1 lot", "G1", 1, None),
                ("G1 long straddle, gamma scalp band 2 lots", "G1", 2, None)]
    rows, all_trades = [], []
    for name, kind, band, sm in variants:
        tr = [r for w, e in cyc if (r := run(days, w, kind, band_lots=band, stop_mult=sm))]
        T = pd.DataFrame(tr)
        T["strategy"] = name
        all_trades.append(T)
        rows.append(summarize(T, name))
        print(rows[-1], flush=True)
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/research_institutional.csv", index=False)
    pd.concat(all_trades).to_csv("data/reports/research_institutional_trades.csv", index=False)
    print(res.to_string(index=False))
