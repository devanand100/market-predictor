"""
Strategy 6 PAPER trading bot (docs/STRATEGY_6_REALTRADE.md). AWS Lambda + EventBridge Scheduler
(Asia/Kolkata) + Dhan market data + MongoDB journal + one Discord message per run. Places NO orders.

Scheduled actions (deploy.py), all IST, holidays skipped in code before any Dhan/Mongo call:
  RENEW_TOKEN    06:00, 18:00 daily        validate / renew the Dhan token stored in Mongo
  HEARTBEAT      08:45                     token, cycle, previous VIX close, plan for today
  ENTRY_0931     09:31                     S2 / E0 / GAP decisions on the 09:30 prices
  TICK           every minute 09:32-15:16  S2/E0 stops, GAP limit target, 15:15 expiry exits, flatten
  EOD            15:40                     equity, margin (model and Dhan), daily report (+ weekly summary)
Manual (invoke.py): STATUS, TEST, INIT, PAUSE, RESUME, FLATTEN, SET_TOKEN {"token"}, CLEAR_TRADE {"tid"}
Event keys: {"action": "...", "date": "YYYY-MM-DD" (optional override), "time": "HH:MM" (optional override)}
"""
import datetime as dt
import hashlib
import json
import os
import sys
import time
import traceback
import uuid

for _p in (os.path.dirname(os.path.abspath(__file__)),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import costs  # noqa: E402
import journal as J  # noqa: E402
import market_calendar as cal  # noqa: E402
import notify as N  # noqa: E402
import rules as R  # noqa: E402
from dhan import Dhan, DhanAuthError  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

env = os.environ.get
CAPITAL = float(env("CAPITAL", "300000"))
SV = env("STRATEGY_VERSION") or ("local-" + hashlib.sha1(json.dumps(R.params(), sort_keys=True).encode()).hexdigest()[:8])
SCHEDULED = {"HEARTBEAT", "ENTRY_0931", "TICK", "EOD"}
NIFTY_ID, VIX_ID = 13, 21
SHORTS = ("S2", "E0")
STOP_MULT = {"S2": R.S2_STOP, "E0": R.E0_STOP}


# ================================================================ helpers
def now_ist(event):
    n = cal.ist_now()
    if event.get("time"):
        h, m = map(int, event["time"].split(":"))
        n = n.replace(hour=h, minute=m)
    return n


def dhan_for(state):
    return Dhan(env("DHAN_CLIENT_ID", ""), (state or {}).get("dhan_token") or env("DHAN_ACCESS_TOKEN", ""))


def equity(state):
    """Realised equity: capital + closed P&L - running costs."""
    return CAPITAL + float(state.get("realised") or 0) - float(state.get("running") or 0)


def unrealised(opened, prices):
    return sum(side * (prices.get(str(sid), fill) - fill) * qty
               for x in opened for sid, side, qty, fill, _, _ in x["legs"])


def refresh_expiries(dh, state):
    """Dhan expiry list + cache of past expiries (Dhan drops an expiry once it has passed)."""
    exps = dh.expiry_list()
    seen = sorted(set(state.get("exp_seen") or []) | set(exps))[-40:]
    return exps, seen


def prev_vix_close(dh, today):
    c = dh.daily_closes(VIX_ID, cal.d(today), days=10)
    return c[-1][1] if c else None


def dhan_margin(dh, ce, pe, lots):
    """Dhan's margin calculator for selling `lots` of the CE/PE pair at their LTPs -> total Rs, or None if unavailable."""
    m = dh.margin([{"sid": ce["sid"], "side": -1, "qty": lots * costs.LOT, "px": ce["ltp"]},
                   {"sid": pe["sid"], "side": -1, "qty": lots * costs.LOT, "px": pe["ltp"]}])
    return round(m["total"]) if m and m.get("total") else None


def booked_margin(item):
    """Margin an open item ties up: Dhan's figure from entry when it was available, else the model's."""
    return item.get("mgd") or item.get("mg") or 0


def chain_leg(chain, K, typ):
    return (chain.get(int(K)) or {}).get(typ.lower()) or {}


def leg_doc(typ, K, leg, side, qty, fill, expiry):
    return {"typ": typ, "K": int(K), "exp": expiry, "sid": leg.get("sid"), "side": side, "qty": qty,
            "ltp": leg.get("ltp"), "bid": leg.get("bid"), "ask": leg.get("ask"), "iv": leg.get("iv"),
            "fill": round(fill, 2)}


def entry_charges(legs, day):
    ch = {}
    for l in legs:
        ch = costs.add_charges(ch, costs.option_fill_charges(l["fill"], l["qty"], l["side"] > 0, day))
    return ch


def compact(tid, sleeve, legs, extra):
    return {"tid": tid, "sl": sleeve, "legs": [[l["sid"], l["side"], l["qty"], l["fill"], l["typ"], l["K"]] for l in legs],
            **extra}


def fmt_legs(legs):
    return "\n".join(f"{'BUY' if l['side'] > 0 else 'SELL'} {l['qty']} × {l['K']} {l['typ']} @ {l['fill']:.2f}"
                     f" (ltp {l['ltp']:.2f}, bid/ask {l.get('bid') or 0:.2f}/{l.get('ask') or 0:.2f})" for l in legs)


# ================================================================ closing positions
def close_position(item, prices, reason, urgent, today, limit=None):
    """Close one open item at current LTPs (or at `limit` for a resting limit order). -> (net, fields)"""
    exits, ch, gross = [], {}, 0.0
    for sid, side, qty, fill, typ, K in item["legs"]:
        px = prices.get(str(sid)) or 0.0
        if limit is not None:
            xf = limit
        elif side < 0:
            xf = R.close_short_fill(px, urgent)
        else:
            xf = R.close_long_fill(px, urgent)
        gross += side * (xf - fill) * qty
        ch = costs.add_charges(ch, costs.option_fill_charges(xf, qty, side < 0, today))
        exits.append({"sid": sid, "typ": typ, "K": K, "ltp": px, "fill": round(xf, 2)})
    tr = J.db().trades.find_one({"_id": item["tid"]}, {"ch_in": 1, "eq_before": 1})
    ch_in = (tr or {}).get("ch_in") or {}
    charges = costs.add_charges(ch_in, ch)
    net = gross - sum(charges.values())
    eq_before = (tr or {}).get("eq_before") or 0
    fields = {"status": "closed", "exit_reason": reason, "exit_d": today, "exit_ts": dt.datetime.now(dt.timezone.utc),
              "exit_legs": exits, "ch_out": ch, "charges": {k: round(v, 2) for k, v in charges.items()},
              "charges_total": round(sum(charges.values()), 2), "gross": round(gross, 2), "net": round(net, 2),
              "px_out": round(sum(x["fill"] for x in exits), 2),
              "ret_pct": round(100 * net / eq_before, 3) if eq_before else None}
    return net, fields


def apply_closes(state, closes, prices, today):
    """closes: [(item, reason, urgent, limit)] -> persists trades, state, notifications. Returns remaining open."""
    if not closes:
        return state.get("open") or []
    gone = {c[0]["tid"] for c in closes}
    remaining = [x for x in state.get("open") or [] if x["tid"] not in gone]
    realised = 0.0
    eq = equity(state)
    for item, reason, urgent, limit in closes:
        net, fields = close_position(item, prices, reason, urgent, today, limit)
        eq += net
        fields["equity_after"] = round(eq)
        J.close_trade(item["tid"], fields)
        realised += net
        lines = "\n".join(f"{'BUY' if s < 0 else 'SELL'} {q} × {K} {t} @ {x['fill']:.2f} (ltp {x['ltp']:.2f})"
                          for (sid, s, q, f, t, K), x in zip(item["legs"], fields["exit_legs"]))
        N.send("trades", f"Exit {item['sl']} — {reason}", lines, N.GREEN if net >= 0 else N.RED, [
            {"name": "Gross", "value": N.fmt_rs(fields["gross"])},
            {"name": "Charges", "value": N.fmt_rs(fields["charges_total"])},
            {"name": "Net P&L", "value": N.fmt_rs(net)},
            {"name": "Trade", "value": item["tid"], "inline": False}])
    J.update_state(set_={"open": remaining}, inc={"realised": realised})
    state["open"] = remaining
    state["realised"] = float(state.get("realised") or 0) + realised
    return remaining


# ================================================================ actions
def a_renew_token(ctx, event):
    st = J.get_state({"dhan_token": 1})
    dh = dhan_for(st)
    valid, info = dh.token_info()
    if not valid:
        raise DhanAuthError(f"Dhan token invalid, cannot renew ({info}). Generate a new token on web.dhan.co and "
                            "run: python strategy6_bot/invoke.py SET_TOKEN --token <token>")
    new = dh.renew()
    ok, validity = dh.token_info(new)
    J.update_state(set_={"dhan_token": new, "token_upd": cal.ist_now().isoformat(), "token_valid": validity})
    return {"renewed": True, "valid_till": validity, "profile_ok": ok}


def plan_for(cyc, vix):
    plan = []
    for sl in ("GAP", "S2", "E0"):
        due, why = R.decide(sl, cyc["i"], cyc["n"], vix)
        if due:
            plan.append(f"{sl} entry at 09:31 ({why})")
        elif R.to_expiry(cyc["i"], cyc["n"]) == {"S2": R.S2_DTE, "E0": 0, "GAP": R.GAP_DTE}[sl]:
            plan.append(f"{sl}: no entry today ({why})")
    if cyc["is_expiry"]:
        plan.append("weekly expiry: open trades exit at 15:15")
    return plan


def a_heartbeat(ctx, event):
    st = J.get_state()
    dh = dhan_for(st)
    valid, validity = dh.token_info()
    exps, seen = refresh_expiries(dh, st)
    today = ctx["d"]
    cyc = cal.weekly_cycle(today, exps, seen)
    vix = prev_vix_close(dh, today)
    J.update_state(set_={"exp_seen": seen})
    plan = plan_for(cyc, vix) if cyc else ["no weekly expiry found"]
    N.send("system", "🫀 Heartbeat" + (" — PAUSED" if st.get("paused") else ""),
           "\n".join(f"• {p}" for p in plan) or "No entries scheduled today.",
           N.ORANGE if (st.get("paused") or not valid) else N.BLUE, [
               {"name": "Dhan token", "value": f"valid till {validity}" if valid else f"INVALID: {validity}"},
               {"name": "Weekly cycle", "value": f"day {cyc['i'] + 1}/{cyc['n']} → {cyc['next_exp']}" if cyc else "?"},
               {"name": "VIX prev close", "value": f"{vix:.2f}" if vix else "?"},
               {"name": "Equity (realised)", "value": N.fmt_rs(equity(st))},
               {"name": "Open trades", "value": str(len(st.get("open") or []))}])
    return {"cycle": cyc, "vix_prev": vix, "plan": plan, "token_valid": valid}


def a_entry(ctx, event, dry=False):
    st = J.get_state()
    dh = dhan_for(st)
    today = ctx["d"]
    exps, seen = refresh_expiries(dh, st)
    cyc = cal.weekly_cycle(today, exps, seen)
    if not cyc:
        raise RuntimeError("no weekly expiry found in Dhan expiry list")
    expiry = cyc["next_exp"]
    vix = prev_vix_close(dh, today)
    nifty = dh.daily_closes(NIFTY_ID, cal.d(today), days=12)
    prev_close = nifty[-1][1] if nifty else None
    due = {sl: R.decide(sl, cyc["i"], cyc["n"], vix) for sl in ("GAP", "S2", "E0")}
    out = {"cycle": {k: cyc[k] for k in ("i", "n", "next_exp", "prev_exp")}, "vix_prev": vix,
           "prev_close": prev_close, "decisions": []}
    opened = list(st.get("open") or [])
    chain, spot = {}, None
    if any(d for d, _ in due.values()):
        q = dh.ltp([l[0] for x in opened for l in x["legs"]])
        spot_ul, chain = dh.option_chain(expiry)
        spot = q["spot"] or spot_ul
        if not spot or not chain:
            raise RuntimeError(f"Dhan returned no NIFTY price / option chain (ltp {q['spot']}, chain {len(chain)} strikes)")
        eq = equity(st) + unrealised(opened, q)            # research sizes on cash + open MTM
    else:
        eq = equity(st)
    used = sum(booked_margin(x) for x in opened)
    paused = bool(st.get("paused"))
    out.update(spot=spot, equity=round(eq), margin_used=round(used))
    new_open, lines = [], []

    for sl in ("GAP", "S2", "E0"):                       # entry days never coincide (4, 2 and 0 sessions to expiry)
        ok, why = due[sl]
        inputs = {"cycle_i": cyc["i"], "cycle_n": cyc["n"], "expiry": expiry, "vix_prev": vix, "spot": spot,
                  "equity": round(eq), "margin_used": round(used)}
        doc = None
        if ok and paused:
            ok, why = False, "bot paused"
        if ok and sl == "GAP":
            typ, gap = R.gap_side(spot, prev_close)
            inputs.update(prev_close=prev_close, gap=round(gap, 5))
            K = R.atm(spot)
            lg = chain_leg(chain, K, typ) if typ else {}
            if typ is None:
                ok, why = False, "gap exactly 0"
            elif not lg.get("sid") or lg.get("ltp", 0) <= 0:
                ok, why = False, f"ATM {K} {typ} has no price in the option chain"
            else:
                lots, cap = R.gap_lots(eq, lg["ltp"], used)
                inputs.update(K=K, typ=typ, lots=lots)
                if lots < 1:
                    ok, why = False, f"one lot ({cap:,.0f}) > {R.F_GAP:.0%} of equity or margin cap"
                else:
                    qty = lots * costs.LOT
                    fill = R.buy_fill(lg["ltp"])
                    legs = [leg_doc(typ, K, lg, +1, qty, fill, expiry)]
                    tgt = round(R.gap_target(fill), 2)
                    doc = {"sleeve": "GAP", "legs": legs, "lots": lots, "qty": qty, "debit": round(fill * qty, 2),
                           "px_in": round(fill, 2), "target": tgt, "margin_model": round(cap * lots)}
                    item_extra = {"tgt": tgt}
                    why = f"gap {gap:+.2%} → buy {lots} × {K} {typ} @ {fill:.2f}, limit {tgt:.2f}; {why}"
        elif ok:
            mult = R.S2_MULT if sl == "S2" else R.E0_MULT
            K = R.atm(spot)
            ce0, pe0 = chain_leg(chain, K, "CE").get("ltp", 0), chain_leg(chain, K, "PE").get("ltp", 0)
            s = R.strangle(spot, ce0, pe0, mult) if ce0 > 0 and pe0 > 0 else None
            if ce0 <= 0 or pe0 <= 0:
                ok, why = False, f"ATM {K} straddle has no price"
            elif s is None:
                ok, why = False, f"width {R.rnd(mult * (ce0 + pe0))} > {R.WINDOW_MAX} (research skips)"
            else:
                ce, pe = chain_leg(chain, s["ce"], "CE"), chain_leg(chain, s["pe"], "PE")
                lots, cap = R.short_lots(eq, spot, R.W_S2 if sl == "S2" else R.W_E0, cyc["is_expiry"], used)
                inputs.update(K=K, S=round(s["S"], 2), w=s["w"], ce_strike=s["ce"], pe_strike=s["pe"], lots=lots,
                              margin_per_lot=round(cap))
                if not ce.get("sid") or not pe.get("sid") or ce.get("ltp", 0) <= 0 or pe.get("ltp", 0) <= 0:
                    ok, why = False, f"strikes {s['ce']} CE / {s['pe']} PE have no price"
                elif lots < 1:
                    ok, why = False, f"equity too small: one lot needs {cap:,.0f} margin"
                if ok and lots >= 1:
                    # real margin check before the (paper) order: Dhan's calculator for exactly these legs
                    avail = R.MAX_UTIL * eq - used
                    mdhan = dhan_margin(dh, ce, pe, lots)
                    if mdhan and mdhan > avail:
                        fit = int(avail // (mdhan / lots))
                        mdhan = dhan_margin(dh, ce, pe, fit) if fit >= 1 else mdhan
                        if fit < 1 or (mdhan and mdhan > avail):
                            ok, why = False, (f"Dhan margin ₹{mdhan:,.0f} for {lots} lot(s) > available ₹{avail:,.0f} "
                                              f"({R.MAX_UTIL:.0%} of equity minus open margin)")
                        else:
                            why = f"Dhan margin cut {lots} → {fit} lot(s); {why}"
                            lots = fit
                    inputs.update(margin_dhan=mdhan, margin_available=round(avail), lots=lots,
                                  margin_check="dhan" if mdhan else "unavailable (model used)")
                if ok and lots >= 1:
                    qty = lots * costs.LOT
                    legs = [leg_doc("CE", s["ce"], ce, -1, qty, R.sell_fill(ce["ltp"]), expiry),
                            leg_doc("PE", s["pe"], pe, -1, qty, R.sell_fill(pe["ltp"]), expiry)]
                    px_in = legs[0]["fill"] + legs[1]["fill"]
                    stop = round(R.stop_level(px_in, STOP_MULT[sl]), 2)
                    doc = {"sleeve": sl, "legs": legs, "lots": lots, "qty": qty, "credit": round(px_in * qty, 2),
                           "px_in": round(px_in, 2), "stop_sum": stop, "margin_model": round(cap * lots),
                           "margin_dhan": mdhan, "margin_available": round(avail)}
                    item_extra = {"stop": stop, "mgd": mdhan}
                    why = (f"sell {lots} × {s['ce']} CE / {s['pe']} PE (S {s['S']:.1f}, w {s['w']}), "
                           f"stop CE+PE ≥ {stop:.2f}, margin Dhan "
                           f"{'₹' + format(mdhan, ',') if mdhan else 'unavailable'} of ₹{avail:,.0f} available; {why}")
        if doc:
            tid = f"P-{sl}-{today}"
            doc.update({"_id": tid, "src": "paper", "status": "open", "key": f"{sl}|{today}", "ckey": f"{sl}|{expiry}",
                        "expiry": expiry, "entry_d": today, "entry_ts": dt.datetime.now(dt.timezone.utc),
                        "exit_plan": expiry, "ch_in": entry_charges(doc["legs"], today), "eq_before": round(eq),
                        "in": inputs})
            used += doc.get("margin_dhan") or doc["margin_model"]
            new_open.append((doc, compact(tid, sl, doc["legs"], {"exp": expiry, "xd": expiry,
                                                                  "mg": doc["margin_model"], **item_extra})))
        outcome = "enter" if doc else "skip"
        out["decisions"].append({"sleeve": sl, "outcome": outcome, "why": why, "in": inputs})
        lines.append(f"**{sl}** {outcome.upper()}: {why}")

    if dry:
        out["dry_run"] = True
        return out
    for d_ in out["decisions"]:
        J.decision(ctx, d_["sleeve"], d_["outcome"], d_["why"], d_["in"])
    for doc, _ in new_open:
        J.insert_trade(ctx, doc)
    J.update_state(set_={"exp_seen": seen},
                   extra={"$push": {"open": {"$each": [c for _, c in new_open]}}} if new_open else None)
    if new_open or any(d for d, _ in due.values()):
        N.send("signals", f"09:30 — {R.to_expiry(cyc['i'], cyc['n'])} session(s) to expiry {expiry}", "\n".join(lines),
               N.BLUE, [{"name": "Spot 09:30", "value": f"{spot:,.2f}" if spot else "?"},
                        {"name": "Prev close", "value": f"{prev_close:,.2f}" if prev_close else "?"},
                        {"name": "VIX prev close", "value": f"{vix:.2f}" if vix else "?"},
                        {"name": "Equity (MTM)", "value": N.fmt_rs(eq)}])
    for doc, _ in new_open:
        N.send("trades", f"Entry {doc['sleeve']}", fmt_legs(doc["legs"]), N.PURPLE, [
            {"name": "Debit" if doc["sleeve"] == "GAP" else "Credit", "value": N.fmt_rs(doc.get("debit") or doc.get("credit"))},
            {"name": "Target" if doc["sleeve"] == "GAP" else "Stop",
             "value": f"≥ {doc['target']:.2f}" if doc["sleeve"] == "GAP" else f"CE+PE ≥ {doc['stop_sum']:.2f}"},
            {"name": "Entry charges", "value": N.fmt_rs(sum(doc["ch_in"].values()))},
            {"name": "Margin model / Dhan", "value": f"{N.fmt_rs(doc['margin_model'])} / "
             + (N.fmt_rs(doc["margin_dhan"]) if doc.get("margin_dhan") else
                ("premium" if doc["sleeve"] == "GAP" else "⚠️ Dhan check unavailable"))},
            *([{"name": "Margin available", "value": N.fmt_rs(doc["margin_available"])}] if doc.get("margin_available") else []),
            {"name": "Trade", "value": doc["_id"], "inline": False}])
    return out


def a_tick(ctx, event):
    st = J.db().state.find_one({"_id": J.STATE_ID}, {"open": 1, "flatten": 1, "dhan_token": 1, "realised": 1, "running": 1}) or {}
    opened = st.get("open") or []
    if not opened and not st.get("flatten"):
        return {"open": 0}
    today = ctx["d"]
    hhmm = now_ist(event).strftime("%H:%M")
    prices = dhan_for(st).ltp([l[0] for x in opened for l in x["legs"]])
    closes = []
    if st.get("flatten"):
        closes = [(x, "flatten", True, None) for x in opened]
    else:
        for x in opened:
            px = {str(l[0]): prices.get(str(l[0])) for l in x["legs"]}
            if any(not v for v in px.values()):
                print(f"{x['tid']}: missing LTP {px}; skipped this tick")
                continue
            if x.get("xd") == today and hhmm >= R.EXIT_HHMM:          # research checks the time exit first
                closes.append((x, "time (expiry 15:15)", False, None))
            elif x["sl"] == "GAP" and R.gap_target_hit(x["tgt"], px[str(x["legs"][0][0])]):
                closes.append((x, "target (2× fill, limit)", False, x["tgt"]))
            elif x["sl"] in SHORTS:
                now_sum = sum(px[str(l[0])] for l in x["legs"])
                if now_sum >= x["stop"]:
                    closes.append((x, f"stop ({STOP_MULT[x['sl']]:g}× credit)", True, None))
    remaining = apply_closes(st, closes, prices, today)
    if st.get("flatten"):
        J.update_state(set_={"flatten": False})
        N.send("system", "🧯 Flatten done", f"Closed {len(closes)} trade(s) at LTP ± urgent slippage.", N.ORANGE)
    # MTM point every 30 minutes to stay inside the Mongo write budget
    if remaining and hhmm[-2:] in ("00", "30"):
        J.update_state(push={"mtm": {"$each": [{"t": hhmm, "m": round(unrealised(remaining, prices))}], "$slice": -80}})
    return {"open": len(remaining), "closed": [c[0]["tid"] for c in closes]}


def a_eod(ctx, event):
    st = J.get_state()
    today = ctx["d"]
    dh = dhan_for(st)
    opened = st.get("open") or []
    prices = dh.ltp([l[0] for x in opened for l in x["legs"]]) if opened else dh.ltp([])
    unreal = unrealised(opened, prices)
    inc = {}
    month = today[:7]
    rc = list(st.get("rc_months") or [])
    if month not in rc:
        inc["running"] = R.MONTHLY_RUNNING_COSTS
        rc.append(month)
        st["running"] = float(st.get("running") or 0) + R.MONTHLY_RUNNING_COSTS
    eq = equity(st)
    mark = eq + unreal
    peak = max(float(st.get("peak") or CAPITAL), mark)
    dd = mark / peak - 1
    margin_dhan = dh.margin([{"sid": l[0], "side": l[1], "qty": l[2], "px": prices.get(str(l[0])) or l[3]}
                             for x in opened for l in x["legs"]]) if opened else None
    margin_model = sum(x.get("mg") or 0 for x in opened)
    closed_today = list(J.db().trades.find({"src": "paper", "exit_d": today}, {"sleeve": 1, "net": 1}))
    by_sleeve = {}
    for t in closed_today:
        by_sleeve[t["sleeve"]] = by_sleeve.get(t["sleeve"], 0) + t.get("net", 0)
    day_doc = {"equity": round(eq), "mark": round(mark), "unrealised": round(unreal),
               "realised_total": round(st.get("realised") or 0), "realised_today": round(sum(by_sleeve.values())),
               "by_sleeve": {k: round(v) for k, v in by_sleeve.items()}, "running_total": round(st.get("running") or 0),
               "running_today": round(inc.get("running", 0)), "closed_n": len(closed_today), "peak": round(peak),
               "dd": round(dd, 5), "open": [{"tid": x["tid"], "sl": x["sl"]} for x in opened],
               "margin_model": round(margin_model), "margin_dhan": margin_dhan, "spot": prices.get("spot"),
               "vix": prices.get("vix"), "mtm_path": st.get("mtm") or [],
               "errors": {k: v for k, v in (st.get("err") or {}).items() if k.startswith(today)}}
    J.upsert_day(ctx, today, day_doc)
    J.update_state(set_={"peak": peak, "rc_months": rc[-24:], "mtm": [], "last_eod": today,
                         "err": {k: v for k, v in (st.get("err") or {}).items() if k >= today}}, inc=inc or None)
    fields = [{"name": "Equity (realised)", "value": N.fmt_rs(eq)}, {"name": "Mark-to-market", "value": N.fmt_rs(mark)},
              {"name": "Today realised", "value": N.fmt_rs(day_doc["realised_today"])},
              {"name": "Drawdown", "value": f"{dd:.2%}"},
              {"name": "Margin model / Dhan", "value": f"{N.fmt_rs(margin_model)} / "
               + (N.fmt_rs(margin_dhan['total']) if margin_dhan else "n/a")},
              {"name": "Spot / VIX", "value": f"{prices.get('spot', 0):,.0f} / {prices.get('vix', 0):.2f}"}]
    if by_sleeve:
        fields.append({"name": "By sleeve", "value": ", ".join(f"{k} {N.fmt_rs(v)}" for k, v in by_sleeve.items()), "inline": False})
    if opened:
        fields.append({"name": "Open", "value": "\n".join(x["tid"] for x in opened)[:1000], "inline": False})
    if inc.get("running"):
        fields.append({"name": "Running costs", "value": f"{N.fmt_rs(R.MONTHLY_RUNNING_COSTS)} charged for {month}"})
    errs = sum(day_doc["errors"].values())
    if errs:
        fields.append({"name": "Errors today", "value": str(errs)})
    N.send("daily_report", f"📊 Daily report {today}", "", N.GREEN if day_doc["realised_today"] >= 0 else N.RED, fields)
    if cal.next_trading_day(today).isocalendar()[1] != cal.d(today).isocalendar()[1]:
        weekly_summary(today)
    return {"equity": eq, "mark": mark, "dd": dd}


def weekly_summary(today):
    monday = cal.d(today) - dt.timedelta(days=cal.d(today).weekday())
    rows = list(J.db().trades.find({"src": "paper", "exit_d": {"$gte": monday.isoformat(), "$lte": today}},
                                   {"sleeve": 1, "net": 1}))
    tot = sum(r.get("net", 0) for r in rows)
    by = {}
    for r in rows:
        by.setdefault(r["sleeve"], []).append(r.get("net", 0))
    lines = [f"**{k}**: {len(v)} trades, {N.fmt_rs(sum(v))}, win {sum(1 for x in v if x > 0)}/{len(v)}" for k, v in by.items()]
    N.send("daily_report", f"🗓️ Week {monday} → {today}", "\n".join(lines) or "No closed trades this week.",
           N.GREEN if tot >= 0 else N.RED, [{"name": "Week net", "value": N.fmt_rs(tot)}])


def a_status(ctx, event):
    st = J.get_state()
    valid, validity = dhan_for(st).token_info()
    info = {"paused": st.get("paused"), "flatten": st.get("flatten"), "capital": CAPITAL, "equity": equity(st),
            "realised": st.get("realised"), "running": st.get("running"), "open": [x["tid"] for x in st.get("open") or []],
            "token_valid": valid, "token_validity": validity, "last_eod": st.get("last_eod"), "strategy_version": SV}
    N.send("system", "ℹ️ Status", "", N.BLUE, [{"name": k, "value": str(v)[:1000]} for k, v in info.items()])
    return info


def a_test(ctx, event):
    """Dry run: Dhan data + rules for today's 09:31 decisions. Writes nothing to trades/state."""
    res = a_entry(ctx, event, dry=True)
    lines = [f"{d['sleeve']}: {d['outcome']} — {d['why']}" for d in res["decisions"]]
    N.send("system", "🧪 TEST (dry run, nothing recorded)", "\n".join(lines)[:3900], N.GREY,
           [{"name": "VIX prev close", "value": str(res.get("vix_prev"))},
            {"name": "Cycle", "value": f"day {res['cycle']['i'] + 1}/{res['cycle']['n']} → {res['cycle']['next_exp']}"}])
    return res


def a_init(ctx, event):
    J.ensure_indexes()
    st = J.get_state()
    if not st.get("dhan_token") and env("DHAN_ACCESS_TOKEN"):
        J.update_state(set_={"dhan_token": env("DHAN_ACCESS_TOKEN"), "token_upd": cal.ist_now().isoformat()})
    return {"indexes": "ok", "state": "ok", "capital": CAPITAL}


def a_set_flag(flag, value, label):
    def f(ctx, event):
        J.update_state(set_={flag: value})
        N.send("system", label, f"Set by {event.get('by', 'invoke.py')}", N.ORANGE)
        return {flag: value}
    return f


def a_set_token(ctx, event):
    tok = event.get("token") or ""
    valid, validity = Dhan(env("DHAN_CLIENT_ID", ""), tok).token_info(tok)
    if not valid:
        raise DhanAuthError(f"SET_TOKEN: token rejected by Dhan ({validity})")
    J.update_state(set_={"dhan_token": tok, "token_upd": cal.ist_now().isoformat(), "token_valid": validity})
    N.send("system", "🔑 Dhan token updated", f"valid till {validity}", N.GREEN)
    return {"valid_till": validity}


def a_clear_trade(ctx, event):
    tid = event.get("tid")
    st = J.get_state({"open": 1})
    J.update_state(set_={"open": [x for x in st.get("open") or [] if x["tid"] != tid]})
    J.close_trade(tid, {"status": "cancelled", "exit_reason": "cleared manually", "exit_d": ctx["d"]})
    N.send("system", "🗑️ Trade cleared", tid, N.ORANGE)
    return {"cleared": tid}


HANDLERS = {
    "RENEW_TOKEN": a_renew_token, "HEARTBEAT": a_heartbeat, "ENTRY_0931": a_entry, "TICK": a_tick, "EOD": a_eod,
    "STATUS": a_status, "TEST": a_test, "INIT": a_init,
    "PAUSE": a_set_flag("paused", True, "⏸️ Paused: no new entries"),
    "RESUME": a_set_flag("paused", False, "▶️ Resumed"),
    "FLATTEN": a_set_flag("flatten", True, "🧯 Flatten requested (next TICK)"),
    "SET_TOKEN": a_set_token, "CLEAR_TRADE": a_clear_trade,
}
NO_LEASE = {"TICK", "STATUS", "TEST", "PAUSE", "RESUME", "FLATTEN"}


# ================================================================ entry point
def lambda_handler(event, context=None):
    event = event or {}
    action = (event.get("action") or "STATUS").upper()
    if action not in HANDLERS:
        return {"error": f"unknown action {action}", "actions": sorted(HANDLERS)}
    today = cal.d(event["date"]).isoformat() if event.get("date") else cal.ist_now().date().isoformat()
    if action in SCHEDULED and not cal.is_trading_day(today):
        return {"skipped": f"{today} is not a trading day"}
    ctx = {"run": uuid.uuid4().hex[:10], "d": today, "sv": SV, "action": action}
    t0 = time.time()
    leased = False
    N.clear()                      # a warm container must not carry another run's messages
    try:
        if action not in NO_LEASE:
            leased = J.acquire_lease(ctx["run"])
            if not leased:
                print(f"{action}: another run holds the lease; exiting quietly")
                return {"skipped": "lease held"}
        result = HANDLERS[action](ctx, event)
        if action != "TICK":
            J.run_summary(ctx, action, "ok", int((time.time() - t0) * 1000))
        return json.loads(json.dumps(result, default=str))
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        first = True
        try:
            first = J.record_error(ctx, action, type(e).__name__, str(e), tb)
            if action != "TICK":
                J.run_summary(ctx, action, "error", int((time.time() - t0) * 1000), {"error": str(e)[:300]})
        except Exception as je:
            print(f"journal unavailable while recording the error: {je}")
        if first:
            hint = ("\nRenew: `python strategy6_bot/invoke.py SET_TOKEN --token <new token>`"
                    if isinstance(e, DhanAuthError) else "")
            N.send("errors", f"🚨 {action} failed: {type(e).__name__}", f"{str(e)[:1500]}{hint}\n```{tb[-1800:]}```",
                   N.RED, [{"name": "Run", "value": ctx["run"]}, {"name": "Date", "value": today}])
        raise
    finally:
        if leased:
            try:
                J.release_lease(ctx["run"])
            except Exception:
                pass
        N.flush()                  # everything from this run goes out as ONE Discord message
