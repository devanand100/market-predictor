"""
Export Strategy 6 at Rs 3L with the paper bot's settings (strategy6_bot/rules.py: short weights 0.55, gap 5%,
Rs 590/month running costs) for the trade-journal dashboard artifact, plus a capacity table: every day the
rules call an entry, whether a fixed Rs 3L account could take it (lots) or why not.
Writes data/reports/strategy6_3L_trades.json, strategy6_3L_daily.json, strategy6_3L_capacity.json
"""
import json
import sys

import pandas as pd

import research_realtrade as rt
from research_realtrade_portfolio import sleeves

sys.path.insert(0, "strategy6_bot")
import rules as R  # noqa: E402

START = 300_000
NAMES = {"S2": "Strangle 2 days out", "E0": "Expiry-day strangle", "GAP": "Gap buy"}

if __name__ == "__main__":
    rt.RUNNING = R.MONTHLY_RUNNING_COSTS
    E, T, acct = rt.replay(sleeves(R.W_S2, R.W_E0, R.F_GAP, vix_hi=R.VIX_HI, vix_lo=R.VIX_LO), START, True)
    days, daily = rt.data()
    info, _ = rt.calendar(days, daily)
    eq_prev = E.set_index("date").equity.shift(1).fillna(START)

    trades = []
    for k, t in enumerate(x for x in acct.trades if x["sleeve"] in NAMES):
        legs = {l["typ"]: l for l in t["legs"]}
        m = t["meta"]
        if t["sleeve"] == "GAP":
            typ = next(iter(legs))
            detail = f"Buy {legs[typ]['K']} {typ} (gap {100 * m['gap']:+.2f}%, VIX {m['vix']:.2f})"
        else:
            detail = f"Sell {legs['CE']['K']} CE / {legs['PE']['K']} PE (VIX {m['vix']:.2f})"
        e, x = t["entry"], t["exit"]
        trades.append({"id": f"{t['sleeve']}-{e}", "sleeve": NAMES[t["sleeve"]], "entry": e, "exit": x,
                       "days_held": (pd.Timestamp(x) - pd.Timestamp(e)).days, "lots": m["lots"], "detail": detail,
                       "exit_reason": t["reason"], "entry_px": round(sum(l["entry"] for l in t["legs"]), 2),
                       "exit_px": round(sum(l["mark"] for l in t["legs"]), 2), "pnl": round(t["pnl"]),
                       "ret_pct": round(100 * t["pnl"] / eq_prev[e], 3), "equity_after": None})
    trades.sort(key=lambda r: (r["exit"], r["entry"]))
    eq = START
    for r in trades:                                   # realised equity after each trade (running costs in daily)
        eq += r["pnl"]
        r["equity_after"] = round(eq)

    T = T.copy()
    closed = T[T.sleeve != "running"].groupby("exit").agg(trade_pnl=("pnl", "sum"), trades_closed=("pnl", "size"))
    run = T[T.sleeve == "running"].groupby("exit").pnl.sum().abs()
    dly = E.set_index("date").join(closed).fillna({"trade_pnl": 0, "trades_closed": 0})
    dly["running_costs"] = run.reindex(dly.index).fillna(0)
    daily_rows = [{"date": d, "trade_pnl": round(r.trade_pnl), "running_costs": round(r.running_costs),
                   "trades_closed": int(r.trades_closed), "equity": round(r.cash), "equity_mark": round(r.equity)}
                  for d, r in dly.iterrows()]

    # capacity of a FIXED Rs 3L account (no compounding): could it take each signal at that day's Nifty and lot size?
    have = {(t["entry"], t["sleeve"]): t for t in acct.trades if t["sleeve"] in NAMES}
    cap_rows = []
    for d, (i, n, exp) in info.items():
        for sl in ("S2", "E0", "GAP"):
            if not R.decide(sl, i, n, daily.loc[d, "vix_prev"])[0]:
                continue
            day, m = days[d], rt.ENTRY_MIN
            spot, lot = float(day.spot[m]), rt.get_lot_size(d)
            K = R.atm(spot)
            ce, pe = rt.real(day, K, "CE", m), rt.real(day, K, "PE", m)
            used, lots, need = 0.0, 0, None
            if ce is None or pe is None:
                outcome, why = "no price", "no 09:30 option price in the data"
            elif sl == "GAP":
                typ, _ = R.gap_side(spot, daily["close"].iloc[daily.index.get_loc(d) - 1])
                p_ = rt.real(day, K, typ, m) if typ else None
                need = p_ * lot if p_ else None
                lots = int(R.F_GAP * START // need) if need else 0
                outcome, why = ("tradable", f"{lots} lot(s)") if lots else ("too small", "one lot costs more than 5% of Rs 3L")
            elif R.strangle(spot, ce, pe, R.S2_MULT if sl == "S2" else R.E0_MULT) is None:
                outcome, why = "too wide", "strike would be more than 450 from ATM (rule skips)"
            else:
                if sl == "E0":                                   # the week's S2 still open at 09:30 on expiry day?
                    s2 = next((t for t in acct.trades if t["sleeve"] == "S2" and t["meta"]["exp"] == exp), None)
                    if s2 and (s2["exit"] > d or (s2["exit"] == d and s2["exit_min"] > m)):
                        used = R.margin_pct(False) * s2["meta"]["spot"] * lot
                need = R.margin_pct(d == exp) * spot * lot
                lots = R.short_lots(START, spot, R.W_S2 if sl == "S2" else R.W_E0, d == exp, used)[0]
                if lots:
                    outcome, why = "tradable", f"{lots} lot(s)"
                elif used and int(R.W_E0 * START // need):
                    outcome, why = "blocked", "the week's 2-days-out strangle is still open (90% margin cap)"
                else:
                    outcome, why = "too small", f"one lot needs Rs {need:,.0f} margin, above 55% of Rs 3L"
            cap_rows.append({"key": f"{d}|{sl}", "date": d, "year": d[:4], "sleeve": NAMES[sl], "outcome": outcome,
                             "lots": lots, "reason": why, "one_lot_rs": round(need) if need else None,
                             "lot_size": lot, "nifty": round(spot, 1), "vix_prev": round(float(daily.loc[d, "vix_prev"]), 2)})

    for name, rows in (("trades", trades), ("daily", daily_rows), ("capacity", cap_rows)):
        p = f"data/reports/strategy6_3L_{name}.json"
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, separators=(",", ":"))
        print(p, len(rows))
    r = rt.report("S6 3L bot settings", E, T, acct, START)
    rt.show(r)
    c = pd.DataFrame(cap_rows)
    print(c.pivot_table(index="year", columns=["sleeve", "outcome"], values="key", aggfunc="count").fillna(0).astype(int))
