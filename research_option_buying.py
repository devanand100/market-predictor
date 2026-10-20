"""
Option BUYING research: intraday opening-range-breakout (ORB) on the nearest weekly Nifty expiry.

Signal (no look-ahead): opening range = spot high/low over minutes [0, OR). The first minute m in
[OR, LAST_ENTRY] whose spot close breaks above the range -> buy CE; below -> buy PE. One trade a
day. Fill at minute m+1 close + slip (the signal is only known at m's close).
Strike = ATM(spot at fill) + offset*50 in the trade direction (offset 0 ATM, 1 = 50 pts OTM, -1 ITM).

Exits (first to happen):
  stop   premium 1-min CLOSE <= entry*(1-stop); fill min(stop, close) - urgent slip
         (engine has no 1-min lows, so the trigger is close-based; the fill still pays the gap)
         'mid' = spot closes back through the opening-range midpoint, fill at close - urgent slip
  target premium 1-min HIGH >= entry*(1+tgt); limit fill at target - slip
  trail  after +50% the stop moves to entry (breakeven)
  time   15:15 close - slip
Ties within a minute go to the stop (conservative).

Output: data/reports/option_buying_trades.csv (per-unit points; costs applied at sizing time)
        data/reports/option_buying_grid.csv   (sized results per config x filter, IS / OOS)
"""
import itertools
import numpy as np
import pandas as pd

from engine import load_days, load_daily, atm_strike
from cost_model import option_fill_cost, slip, MONTHLY_RUNNING_COSTS

EXIT_MIN = 360          # 15:15
LAST_ENTRY = 225        # 13:00
ORS = [15, 30, 60]
OFFSETS = [-1, 0, 1, 2]
STOPS = [0.25, 0.4, 0.6, "mid"]
TARGETS = [0.5, 1.0, 2.0, None]
TRAILS = [False, True]
LOT = 65
START = 300_000
IS_END = "2023-12-31"


def sim_long(cl, hi, spot, entry, stop, tgt, trail, or_mid, up):
    """cl/hi: premium closes/highs from the minute AFTER the fill to EXIT_MIN."""
    n = len(cl)
    INF = n + 1
    # target
    t_hit = INF
    if tgt is not None:
        tp = entry * (1 + tgt)
        h = np.nonzero(hi >= tp)[0]
        t_hit = h[0] if len(h) else INF
    # trail activation
    tr_on = INF
    if trail:
        h = np.nonzero(hi >= entry * 1.5)[0]
        tr_on = h[0] if len(h) else INF
    # stop
    if stop == "mid":
        bad = (spot < or_mid) if up else (spot > or_mid)
        s_lvl = np.full(n, -np.inf)
    else:
        bad = np.zeros(n, bool)
        s_lvl = np.full(n, entry * (1 - stop))
    if tr_on < INF:
        s_lvl[tr_on + 1:] = np.maximum(s_lvl[tr_on + 1:], entry)
        bad[tr_on + 1:] |= cl[tr_on + 1:] <= entry
    hit = np.nonzero(bad | (cl <= s_lvl))[0]
    s_hit = hit[0] if len(hit) else INF
    if s_hit <= t_hit and s_hit < INF:
        lvl = s_lvl[s_hit] if np.isfinite(s_lvl[s_hit]) else cl[s_hit]
        px = min(lvl, cl[s_hit]) if not bad[s_hit] else cl[s_hit]
        return max(0.05, px - slip(px, urgent=True)), s_hit, "stop"
    if t_hit < INF:
        tp = entry * (1 + tgt)
        return tp - slip(tp), t_hit, "target"
    px = cl[-1]
    return max(0.05, px - slip(px)), n - 1, "time"


def build_trades(days, daily):
    rows = []
    for date, day in days.items():
        ts = pd.Timestamp(date)
        if ts not in daily.index:
            continue
        f = daily.loc[ts]
        sp = day.spot
        for OR in ORS:
            hi_or, lo_or = sp[:OR].max(), sp[:OR].min()
            mid = (hi_or + lo_or) / 2
            seg = sp[OR:LAST_ENTRY + 1]
            brk = np.nonzero((seg > hi_or) | (seg < lo_or))[0]
            if not len(brk):
                continue
            m = OR + brk[0]
            up = sp[m] > hi_or
            e = m + 1
            typ = "CE" if up else "PE"
            K0 = atm_strike(sp[e])
            for off in OFFSETS:
                k = K0 + (off * 50 if up else -off * 50)
                p0 = day.px(k, typ, e)
                if p0 < 2:
                    continue
                entry = p0 + slip(p0)
                cl = day.series(k, typ, e + 1, EXIT_MIN)
                hi = day.series(k, typ, e + 1, EXIT_MIN, high=True)
                spx = sp[e + 1:EXIT_MIN + 1]
                for stop, tgt, trail in itertools.product(STOPS, TARGETS, TRAILS):
                    if trail and tgt is not None and tgt <= 0.5:
                        continue
                    xp, xm, why = sim_long(cl, hi, spx, entry, stop, tgt, trail, mid, up)
                    rows.append((date, OR, off, str(stop), str(tgt), trail, typ, e, entry, xp,
                                 why, f["vix_0920"], bool(f["is_expiry"]), (hi_or - lo_or) / sp[0]))
    return pd.DataFrame(rows, columns=["date", "OR", "off", "stop", "tgt", "trail", "typ", "entry_min",
                                       "entry", "exit", "why", "vix", "expiry", "or_width"])


def size_run(tr, risk_frac, start=START, running=0.0):
    """Spend `risk_frac` of equity on premium per trade (max loss = premium), whole lots of 65.
    Returns daily-ish equity series (one point per trade) and stats."""
    eq = start
    out = []
    for r in tr.itertuples(index=False):
        lots = int(eq * risk_frac // (r.entry * LOT))
        if lots < 1:
            out.append((r.date, eq, 0.0))
            continue
        q = lots * LOT
        pnl = (r.exit - r.entry) * q - option_fill_cost(r.entry, q, True, r.date) \
            - option_fill_cost(r.exit, q, False, r.date)
        eq += pnl
        out.append((r.date, eq, pnl))
    led = pd.DataFrame(out, columns=["date", "equity", "pnl"])
    if running and len(led):
        # Deduct fixed monthly costs on the first trade of each month
        months = pd.to_datetime(led["date"]).dt.to_period("M")
        first = ~months.duplicated()
        led["equity"] = led["equity"] - running * first.cumsum()
    return led


def stats(led, start=START, d0=None, d1=None):
    if led.empty:
        return dict(cagr=np.nan, dd=np.nan, n=0)
    e = np.concatenate([[start], led["equity"].to_numpy()])
    t0 = pd.Timestamp(d0 or led["date"].iloc[0])
    t1 = pd.Timestamp(d1 or led["date"].iloc[-1])
    yrs = max((t1 - t0).days / 365.25, 0.25)
    cagr = (e[-1] / start) ** (1 / yrs) - 1 if e[-1] > 0 else -1
    dd = (e / np.maximum.accumulate(e) - 1).min()
    traded = led[led["pnl"] != 0]
    win = (traded["pnl"] > 0).mean() if len(traded) else np.nan
    return dict(cagr=round(100 * cagr, 1), dd=round(100 * dd, 1), n=len(traded),
                win=round(100 * win, 1), final=round(e[-1]))


FILTERS = {
    "all": lambda t: t,
    "vix>15": lambda t: t[t.vix > 15],
    "vix<=15": lambda t: t[t.vix <= 15],
    "expiry": lambda t: t[t.expiry],
    "non_expiry": lambda t: t[~t.expiry],
    "expiry&vix>15": lambda t: t[t.expiry & (t.vix > 15)],
    "non_exp&vix>15": lambda t: t[~t.expiry & (t.vix > 15)],
}


def grid(T, risk_frac=0.05):
    res = []
    keys = ["OR", "off", "stop", "tgt", "trail"]
    for k, g in T.groupby(keys):
        for fname, fn in FILTERS.items():
            s = fn(g).sort_values("date")
            if len(s) < 30:
                continue
            ret = (s.exit - s.entry) / s.entry
            row = dict(zip(keys, k), filt=fname, n=len(s), avg_ret=round(100 * ret.mean(), 2))
            for per, part in [("IS", s[s.date <= IS_END]), ("OOS", s[s.date > IS_END])]:
                st = stats(size_run(part, risk_frac), d0="2021-01-01" if per == "IS" else "2024-01-01",
                           d1=IS_END if per == "IS" else part.date.max())
                row.update({f"{per}_{a}": b for a, b in st.items() if a in ("cagr", "dd", "n")})
                row[f"{per}_avg"] = round(100 * ((part.exit - part.entry) / part.entry).mean(), 2)
            res.append(row)
    return pd.DataFrame(res)


if __name__ == "__main__":
    import time
    t = time.time()
    days, daily = load_days(), load_daily()
    T = build_trades(days, daily)
    T.to_csv("data/reports/option_buying_trades.csv", index=False)
    print(len(T), "trade rows in", round(time.time() - t), "s")
    G = grid(T)
    G.to_csv("data/reports/option_buying_grid.csv", index=False)
    print(len(G), "grid rows in", round(time.time() - t), "s")
    pd.set_option("display.width", 250)
    print("\nAverage return per trade (% of premium, before costs) by filter, all configs:")
    print(G.groupby("filt")[["IS_avg", "OOS_avg"]].describe()[[("IS_avg", "50%"), ("IS_avg", "max"),
                                                              ("OOS_avg", "50%"), ("OOS_avg", "max")]])
    print("\nShare of configs with positive sized CAGR:")
    print(G.groupby("filt").apply(lambda g: pd.Series({"IS>0": (g.IS_cagr > 0).mean(),
                                                       "OOS>0": (g.OOS_cagr > 0).mean(),
                                                       "both>0": ((g.IS_cagr > 0) & (g.OOS_cagr > 0)).mean()})))
    print("\nTop 15 by IS CAGR (selection on 2021-23 only) with their OOS:")
    print(G.sort_values("IS_cagr", ascending=False).head(15).to_string(index=False))
