"""
Three-sleeve PAPER trading bot (Strategy 5, docs/PORTFOLIO_STRATEGY.md). AWS Lambda + EventBridge
Scheduler (Asia/Kolkata) + Dhan market data + MongoDB journal + Discord webhooks. Places NO orders.

Scheduled actions (deploy.py), all IST, holidays skipped in code before any Dhan/Mongo call:
  RENEW_TOKEN    06:00, 18:00 daily      validate / renew the Dhan token stored in Mongo
  RECONCILE      08:15                   replay yesterday on Dhan 1-min data with the research rules
  HEARTBEAT      08:45                   token, expiries, cycle, plan for today -> #system
  SNAPSHOT_0920  09:21                   VIX and spot at the 09:20 bar
  ENTRY_0931     09:31                   L2 strangle tranche + gap buy decisions (09:30 bar)
  MONTHLY_1015   10:15                   monthly straddle exit / entry (research 09:15 60-min bar close)
  TICK           every 5 min 09:36-15:31 L2 tranche stops, gap target, 15:15 expiry exits, flatten
  EOD            15:40                   equity, margin, daily report (+ weekly summary)
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
CAPITAL = float(env("CAPITAL", "1000000"))
SV = env("STRATEGY_VERSION") or ("local-" + hashlib.sha1(json.dumps(R.params(), sort_keys=True).encode()).hexdigest()[:8])
SCHEDULED = {"RECONCILE", "HEARTBEAT", "SNAPSHOT_0920", "ENTRY_0931", "MONTHLY_1015", "TICK", "EOD"}
TIME_EXIT = "15:16"          # first 5-min tick after the 15:15 bar closes


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
    return CAPITAL + float(state.get("realised") or 0) - float(state.get("running") or 0)


def refresh_expiries(dh, state):
    """Dhan expiry list + cache of past expiries (Dhan drops an expiry once it has passed)."""
    exps = dh.expiry_list()
    seen = sorted(set(state.get("exp_seen") or []) | set(exps))[-40:]
    return exps, seen


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


def new_tid(sleeve, day, suffix=""):
    return f"P-{sleeve}-{day}{suffix}"


def fmt_legs(legs):
    return "\n".join(f"{'BUY' if l['side'] > 0 else 'SELL'} {l['qty']} × {l['K']} {l['typ']} @ {l['fill']:.2f}"
                     f" (ltp {l['ltp']:.2f}, bid/ask {l.get('bid') or 0:.2f}/{l.get('ask') or 0:.2f})" for l in legs)


# ================================================================ closing positions
def close_position(ctx, item, prices, reason, urgent, today):
    """Close one open item at current LTPs. -> (net P&L, trade fields, message lines)"""
    exits, ch, gross = [], {}, 0.0
    for sid, side, qty, fill, typ, K in item["legs"]:
        px = prices.get(str(sid)) or 0.0
        if side < 0:
            xf = R.close_short_fill(px, urgent)
        else:
            xf = R.close_long_fill(px, urgent)
        gross += side * (xf - fill) * qty
        ch = costs.add_charges(ch, costs.option_fill_charges(xf, qty, side < 0, today))
        exits.append({"sid": sid, "typ": typ, "K": K, "ltp": px, "fill": round(xf, 2)})
    tr = J.db().trades.find_one({"_id": item["tid"]}, {"ch_in": 1})
    ch_in = (tr or {}).get("ch_in") or {}
    charges = costs.add_charges(ch_in, ch)
    net = gross - sum(charges.values())
    fields = {"status": "closed", "exit_reason": reason, "exit_d": today, "exit_ts": dt.datetime.now(dt.timezone.utc),
              "exit_legs": exits, "ch_out": ch, "charges": {k: round(v, 2) for k, v in charges.items()},
              "charges_total": round(sum(charges.values()), 2), "gross": round(gross, 2), "net": round(net, 2)}
    return net, fields


def apply_closes(ctx, state, closes, prices, today, title_prefix=""):
    """closes: [(item, reason, urgent)] -> persists trades, state, notifications. Returns remaining open list."""
    if not closes:
        return state.get("open") or []
    remaining = [x for x in state.get("open") or [] if x["tid"] not in {c[0]["tid"] for c in closes}]
    realised = 0.0
    for item, reason, urgent in closes:
        net, fields = close_position(ctx, item, prices, reason, urgent, today)
        J.close_trade(item["tid"], fields)
        realised += net
        color = N.GREEN if net >= 0 else N.RED
        lines = "\n".join(f"{'BUY' if s < 0 else 'SELL'} {q} × {K} {t} @ {x['fill']:.2f} (ltp {x['ltp']:.2f})"
                          for (sid, s, q, f, t, K), x in zip(item["legs"], fields["exit_legs"]))
        N.send("trades", f"{title_prefix}Exit {item['sl']} — {reason}", lines, color, [
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
                            "run: python three_sleeve_bot/invoke.py SET_TOKEN --token <token>")
    new = dh.renew()
    ok, validity = dh.token_info(new)
    J.update_state(set_={"dhan_token": new, "token_upd": cal.ist_now().isoformat(), "token_valid": validity})
    return {"renewed": True, "valid_till": validity, "profile_ok": ok}


def a_heartbeat(ctx, event):
    st = J.get_state()
    dh = dhan_for(st)
    valid, validity = dh.token_info()
    exps, seen = refresh_expiries(dh, st)
    today = ctx["d"]
    cyc = cal.weekly_cycle(today, exps, seen)
    J.update_state(set_={"exp_seen": seen})
    plan = []
    if cyc:
        if R.l2_enters_today(cyc["i"], cyc["n"]):
            plan.append(f"L2 tranche {cyc['i'] + 1} at 09:31")
        if cyc["n"] >= 5 and cyc["i"] == cyc["n"] - 5:
            plan.append("gap-buy check at 09:31 (needs VIX ≤ 15)")
        if cyc["is_expiry"]:
            plan.append("weekly expiry: L2/gap exits at 15:15")
    mon = cal.monthly_expiries(exps)
    for e in mon:
        if R.mon_entry_due(cal.dte(e, today), cal.dte(e, cal.prev_trading_day(today))):
            plan.append(f"monthly straddle entry check 10:15 (expiry {e}, VRP ≥ 2)")
    if any(x["sl"] == "MON" and R.mon_exit_due(cal.dte(x["exp"], today)) for x in st.get("open") or []):
        plan.append("monthly straddle exit 10:15")
    N.send("system", "🫀 Heartbeat" + (" — PAUSED" if st.get("paused") else ""),
           ("\n".join(f"• {p}" for p in plan) or "No scheduled entries today."),
           N.ORANGE if (st.get("paused") or not valid) else N.BLUE, [
               {"name": "Dhan token", "value": f"valid till {validity}" if valid else f"INVALID: {validity}"},
               {"name": "Weekly cycle", "value": f"day {cyc['i'] + 1}/{cyc['n']} → {cyc['next_exp']}" if cyc else "?"},
               {"name": "Equity (realised)", "value": N.fmt_rs(equity(st))},
               {"name": "Open trades", "value": str(len(st.get("open") or []))},
               {"name": "Recon pending", "value": ", ".join(st.get("recon_pending") or []) or "none"}])
    if cyc and cyc.get("prev_exp_guessed"):
        print("previous expiry guessed (empty cache) – cycle day count may be off on the first week")
    return {"cycle": cyc, "plan": plan, "token_valid": valid}


def a_snapshot(ctx, event):
    st = J.get_state({"dhan_token": 1})
    q = dhan_for(st).ltp([])
    if not q["vix"] or not q["spot"]:
        raise RuntimeError(f"Dhan LTP returned no index prices at 09:20 (spot {q['spot']}, VIX {q['vix']})")
    J.update_state(set_={"snap": {"d": ctx["d"], "vix_0920": q["vix"], "spot_0920": q["spot"],
                                  "t": now_ist(event).strftime("%H:%M:%S")}})
    return {"vix_0920": q["vix"], "spot_0920": q["spot"]}


def a_entry(ctx, event, dry=False):
    st = J.get_state()
    dh = dhan_for(st)
    today = ctx["d"]
    exps, seen = refresh_expiries(dh, st)
    cyc = cal.weekly_cycle(today, exps, seen)
    if not cyc:
        raise RuntimeError("no weekly expiry found in Dhan expiry list")
    q = dh.ltp([])
    expiry = cyc["next_exp"]
    spot_ul, chain = dh.option_chain(expiry)
    spot, vix_now = q["spot"] or spot_ul, q["vix"] or None
    if not spot or not chain:
        raise RuntimeError(f"Dhan returned no NIFTY price / option chain (ltp {q['spot']}, chain {len(chain)} strikes)")
    closes = dh.daily_closes(13, cal.d(today), days=12)
    prev_close = closes[-1][1] if closes else None
    snap = st.get("snap") or {}
    vix_0920 = snap.get("vix_0920") if snap.get("d") == today else None
    vix_src = "09:20 snapshot" if vix_0920 else "live (09:20 snapshot missing)"
    vix_0920 = vix_0920 or vix_now
    eq = equity(st)
    paused = bool(st.get("paused"))
    out = {"cycle": {k: cyc[k] for k in ("i", "n", "next_exp", "prev_exp")}, "spot": spot, "prev_close": prev_close,
           "vix_0920": vix_0920, "equity": eq, "decisions": []}
    new_open, signal_lines = [], []
    set_extra = {"exp_seen": seen}

    # ---------------- L2 strangle tranche
    l2_state = st.get("l2") or {}
    inputs = {"spot": spot, "cycle_i": cyc["i"], "cycle_n": cyc["n"], "expiry": expiry, "equity": round(eq)}
    if not R.l2_enters_today(cyc["i"], cyc["n"]):
        dec = ("skip", f"no tranche on cycle day {cyc['i'] + 1}/{cyc['n']}")
    elif paused:
        dec = ("skip", "bot paused")
    else:
        if cyc["i"] == 0:
            n_total = R.l2_total_lots(eq, spot)
            l2_state = {"cycle": expiry, "N": n_total, "per": n_total // 3, "first": today}
            set_extra["l2"] = l2_state
        if l2_state.get("cycle") != expiry:
            dec = ("skip", "cycle started before the bot (no tranche-0 sizing)")
        elif l2_state.get("per", 0) < 1:
            dec = ("skip", f"equity too small: {l2_state.get('N', 0)} lots < 3")
        else:
            K = R.atm(spot)
            ce_atm, pe_atm = chain_leg(chain, K, "CE").get("ltp", 0), chain_leg(chain, K, "PE").get("ltp", 0)
            s = R.l2_strikes(spot, ce_atm, pe_atm)
            ce, pe = chain_leg(chain, s["ce"], "CE"), chain_leg(chain, s["pe"], "PE")
            inputs.update(K=K, S=round(s["S"], 2), w=s["w"], ce_strike=s["ce"], pe_strike=s["pe"],
                          lots=l2_state["per"], N=l2_state["N"])
            if not ce.get("sid") or not pe.get("sid") or ce.get("ltp", 0) <= 0 or pe.get("ltp", 0) <= 0:
                dec = ("skip", f"strikes {s['ce']}CE/{s['pe']}PE missing in option chain")
            elif cyc["i"] == 0 and min(R.sell_fill(ce["ltp"]), R.sell_fill(pe["ltp"])) < R.MIN_SHORT_PX:
                dec = ("skip", "short leg below Rs 1 (research skips the cycle)")
            else:
                qty = l2_state["per"] * costs.LOT
                legs = [leg_doc("CE", s["ce"], ce, -1, qty, R.sell_fill(ce["ltp"]), expiry),
                        leg_doc("PE", s["pe"], pe, -1, qty, R.sell_fill(pe["ltp"]), expiry)]
                tid = new_tid("L2", today, f"-t{cyc['i']}")
                credit = (legs[0]["fill"] + legs[1]["fill"]) * qty
                ch = entry_charges(legs, today)
                doc = {"_id": tid, "src": "paper", "status": "open", "sleeve": "L2", "key": f"L2|{today}|t{cyc['i']}",
                       "ckey": f"L2|{l2_state['first']}", "tranche": cyc["i"], "expiry": expiry, "entry_d": today,
                       "entry_ts": dt.datetime.now(dt.timezone.utc), "legs": legs, "lots": l2_state["per"], "qty": qty,
                       "credit": round(credit, 2), "stop_sum": round(2.5 * (legs[0]["fill"] + legs[1]["fill"]), 2),
                       "exit_plan": expiry, "ch_in": ch, "eq_before": round(eq),
                       "margin_model": round(R.margin_model(spot, l2_state["per"])), "in": inputs}
                new_open.append((doc, compact(tid, "L2", legs, {"exp": expiry, "xd": expiry,
                                                                "stop": doc["stop_sum"]})))
                dec = ("enter", f"tranche {cyc['i'] + 1}: sell {s['ce']} CE / {s['pe']} PE (S {s['S']:.1f}, w {s['w']})")
    out["decisions"].append({"sleeve": "L2", "outcome": dec[0], "why": dec[1], "in": inputs})
    signal_lines.append(f"**L2** {dec[0].upper()}: {dec[1]}")

    # ---------------- gap buy
    take, typ, gap, why = R.gap_decision(cyc["n"], cyc["i"], spot, prev_close, vix_0920)
    g_in = {"spot_0930": spot, "prev_close": prev_close, "gap": round(gap, 5), "vix_0920": vix_0920,
            "vix_src": vix_src, "cycle_i": cyc["i"], "cycle_n": cyc["n"], "equity": round(eq)}
    if take and paused:
        take, why = False, "bot paused"
    if take:
        K = R.atm(spot)
        lg = chain_leg(chain, K, typ)
        if not lg.get("sid") or lg.get("ltp", 0) <= 0:
            take, why = False, f"ATM {K} {typ} missing in option chain"
        else:
            fill = R.buy_fill(lg["ltp"])
            lots = R.gap_lots(eq, fill)
            g_in.update(K=K, typ=typ, lots=lots)
            if lots < 1:
                take, why = False, f"one lot ({fill * costs.LOT:,.0f}) > 6% of equity"
            else:
                qty = lots * costs.LOT
                legs = [leg_doc(typ, K, lg, +1, qty, fill, expiry)]
                tid = new_tid("GAP", today)
                doc = {"_id": tid, "src": "paper", "status": "open", "sleeve": "GAP", "key": f"GAP|{today}",
                       "ckey": f"GAP|{today}", "expiry": expiry, "entry_d": today,
                       "entry_ts": dt.datetime.now(dt.timezone.utc), "legs": legs, "lots": lots, "qty": qty,
                       "debit": round(fill * qty, 2), "target": round((1 + R.GAP_TP) * fill, 2), "exit_plan": expiry,
                       "ch_in": entry_charges(legs, today), "eq_before": round(eq), "margin_model": round(fill * qty),
                       "in": g_in}
                new_open.append((doc, compact(tid, "GAP", legs, {"exp": expiry, "xd": expiry, "tgt": doc["target"]})))
                why = f"{why}; {lots} lot(s) @ {fill:.2f}, target {doc['target']:.2f}"
    out["decisions"].append({"sleeve": "GAP", "outcome": "enter" if take else "skip", "why": why, "in": g_in})
    signal_lines.append(f"**GAP** {'ENTER' if take else 'SKIP'}: {why}")

    if dry:
        out["dry_run"] = True
        return out
    for d_ in out["decisions"]:
        J.decision(ctx, d_["sleeve"], d_["outcome"], d_["why"], d_["in"])
    for doc, _ in new_open:
        J.insert_trade(ctx, doc)
    J.update_state(set_=set_extra, extra={"$push": {"open": {"$each": [c for _, c in new_open]}}} if new_open else None)
    N.send("signals", f"09:30 signals — cycle day {cyc['i'] + 1}/{cyc['n']} (expiry {expiry})", "\n".join(signal_lines),
           N.BLUE, [{"name": "Spot 09:30", "value": f"{spot:,.2f}"},
                    {"name": "Prev close", "value": f"{prev_close:,.2f}" if prev_close else "?"},
                    {"name": "Gap", "value": f"{gap:+.2%}"}, {"name": "VIX 09:20", "value": f"{vix_0920:.2f}"},
                    {"name": "Equity", "value": N.fmt_rs(eq)}])
    for doc, _ in new_open:
        N.send("trades", f"Entry {doc['sleeve']}" + (f" tranche {doc['tranche'] + 1}" if doc["sleeve"] == "L2" else ""),
               fmt_legs(doc["legs"]), N.PURPLE, [
                   {"name": "Credit" if doc["sleeve"] == "L2" else "Debit",
                    "value": N.fmt_rs(doc.get("credit") or doc.get("debit"))},
                   {"name": "Stop" if doc["sleeve"] == "L2" else "Target",
                    "value": f"CE+PE ≥ {doc['stop_sum']:.2f}" if doc["sleeve"] == "L2" else f"≥ {doc['target']:.2f}"},
                   {"name": "Entry charges", "value": N.fmt_rs(sum(doc["ch_in"].values()))},
                   {"name": "Margin (model)", "value": N.fmt_rs(doc["margin_model"])},
                   {"name": "Trade", "value": doc["_id"], "inline": False}])
    return out


def a_monthly(ctx, event, dry=False):
    st = J.get_state()
    dh = dhan_for(st)
    today = ctx["d"]
    exps, seen = refresh_expiries(dh, st)
    mon = cal.monthly_expiries(exps)
    out = {"monthly_expiries": mon[:4], "decisions": []}
    # ---------------- exits first
    closes, prices = [], {}
    due = [x for x in st.get("open") or [] if x["sl"] == "MON" and R.mon_exit_due(cal.dte(x["exp"], today))]
    if due and not dry:
        prices = dh.ltp([l[0] for x in due for l in x["legs"]], with_index=False)
        closes = [(x, "time (21 DTE)", False) for x in due]
        apply_closes(ctx, st, closes, prices, today)
    out["exits"] = [x["tid"] for x in due]
    # ---------------- entry
    eq = equity(st)
    prev = cal.prev_trading_day(today)
    for e in mon:
        dte_t, dte_p = cal.dte(e, today), cal.dte(e, prev)
        if not R.mon_entry_due(dte_t, dte_p):
            continue
        key = f"MON|{e}"
        inputs = {"expiry": e, "dte": dte_t, "equity": round(eq)}
        if J.db().trades.find_one({"src": "paper", "key": key}, {"_id": 1}):
            continue
        vix_c = dh.daily_closes(21, cal.d(today), days=10)
        nifty_c = dh.daily_closes(13, cal.d(today), days=45)
        vix_prev = vix_c[-1][1] if vix_c else None
        v, rv = R.vrp(vix_prev, [c for _, c in nifty_c])
        inputs.update(vix_prev_close=vix_prev, rv20=round(rv, 3) if rv else None, vrp=round(v, 3) if v is not None else None)
        if v is None:
            dec = ("skip", "VRP unavailable (daily history missing)")
        elif v < R.VRP_MIN:
            dec = ("skip", f"VRP {v:.2f} < {R.VRP_MIN:g} (VIX {vix_prev:.2f} − RV20 {rv:.2f})")
        elif st.get("paused"):
            dec = ("skip", "bot paused")
        else:
            time.sleep(3)    # option-chain rate limit (1 request / 3 s)
            spot_ul, chain = dh.option_chain(e)
            spot = spot_ul or dh.ltp([])["spot"]
            if not spot or not chain:
                raise RuntimeError(f"Dhan returned no price / option chain for monthly expiry {e}")
            simple = {k: (v_["ce"]["ltp"], v_["pe"]["ltp"]) for k, v_ in chain.items()
                      if v_["ce"].get("ltp", 0) > 0 and v_["pe"].get("ltp", 0) > 0}
            K, F = R.mon_strike(simple, spot)
            lots = R.mon_lots(eq, spot)
            inputs.update(spot=spot, forward=round(F, 2) if F else None, K=K, lots=lots)
            if K is None:
                dec = ("skip", "no strike with both CE and PE prices")
            elif lots < 1:
                dec = ("skip", f"equity too small for one lot ({R.margin_model(spot, 1):,.0f} margin)")
            else:
                qty = lots * costs.LOT
                ce, pe = chain_leg(chain, K, "CE"), chain_leg(chain, K, "PE")
                legs = [leg_doc("CE", K, ce, -1, qty, R.sell_fill(ce["ltp"]), e),
                        leg_doc("PE", K, pe, -1, qty, R.sell_fill(pe["ltp"]), e)]
                tid = new_tid("MON", today)
                doc = {"_id": tid, "src": "paper", "status": "open", "sleeve": "MON", "key": key, "ckey": f"MON|{today}",
                       "expiry": e, "entry_d": today, "entry_ts": dt.datetime.now(dt.timezone.utc), "legs": legs,
                       "lots": lots, "qty": qty, "credit": round((legs[0]["fill"] + legs[1]["fill"]) * qty, 2),
                       "exit_plan": f"first session with DTE ≤ {R.MON_EXIT_DTE}", "ch_in": entry_charges(legs, today),
                       "eq_before": round(eq), "margin_model": round(R.margin_model(spot, lots)), "in": inputs}
                dec = ("enter", f"sell {K} straddle ×{lots} lots, VRP {v:.2f}")
                if not dry:
                    J.insert_trade(ctx, doc)
                    J.update_state(set_={"exp_seen": seen}, extra={"$push": {"open": compact(tid, "MON", legs, {"exp": e, "xd": None})}})
                    N.send("trades", "Entry MON straddle", fmt_legs(legs), N.PURPLE, [
                        {"name": "Credit", "value": N.fmt_rs(doc["credit"])},
                        {"name": "Expiry / DTE", "value": f"{e} / {dte_t}"},
                        {"name": "VRP", "value": f"{v:.2f} (VIX {vix_prev:.2f} − RV20 {rv:.2f})"},
                        {"name": "Margin (model)", "value": N.fmt_rs(doc["margin_model"])},
                        {"name": "Trade", "value": tid, "inline": False}])
        out["decisions"].append({"sleeve": "MON", "outcome": dec[0], "why": dec[1], "in": inputs})
        if not dry:
            J.decision(ctx, "MON", dec[0], dec[1], inputs)
            N.send("signals", f"Monthly straddle {dec[0].upper()}", dec[1], N.BLUE if dec[0] == "enter" else N.GREY)
    return out


def a_tick(ctx, event):
    st = J.db().state.find_one({"_id": J.STATE_ID}, {"open": 1, "flatten": 1, "dhan_token": 1, "realised": 1}) or {}
    opened = st.get("open") or []
    if not opened and not st.get("flatten"):
        return {"open": 0}
    today = ctx["d"]
    hhmm = now_ist(event).strftime("%H:%M")
    dh = dhan_for(st)
    prices = dh.ltp([l[0] for x in opened for l in x["legs"]])
    closes = []
    if st.get("flatten"):
        closes = [(x, "flatten", True) for x in opened]
    else:
        for x in opened:
            px = {str(l[0]): prices.get(str(l[0])) for l in x["legs"]}
            if any(not v for v in px.values()):
                print(f"{x['tid']}: missing LTP {px}; skipped this tick")
                continue
            if x["sl"] in ("L2", "GAP") and x.get("xd") == today and hhmm >= TIME_EXIT:
                closes.append((x, "time (expiry 15:15)", False))
            elif x["sl"] == "L2":
                ce = next(px[str(l[0])] for l in x["legs"] if l[4] == "CE")
                pe = next(px[str(l[0])] for l in x["legs"] if l[4] == "PE")
                fce = next(l[3] for l in x["legs"] if l[4] == "CE")
                fpe = next(l[3] for l in x["legs"] if l[4] == "PE")
                if R.l2_stop_hit(fce, fpe, ce, pe):
                    closes.append((x, "stop (1.5× credit)", True))
            elif x["sl"] == "GAP":
                l = x["legs"][0]
                if R.gap_target_hit(l[3], px[str(l[0])]):
                    closes.append((x, "target (2× fill)", False))
    remaining = apply_closes(ctx, st, closes, prices, today)
    if st.get("flatten"):
        J.update_state(set_={"flatten": False})
        N.send("system", "🧯 Flatten done", f"Closed {len(closes)} trade(s) at LTP ± urgent slippage.", N.ORANGE)
    # MTM point every 30 minutes (:01 / :31) to stay inside the Mongo write budget (< 40 writes a day)
    if remaining and hhmm[-2:] in ("01", "31"):
        mtm = sum(side * (prices.get(str(sid), fill) - fill) * qty
                  for x in remaining for sid, side, qty, fill, _, _ in x["legs"])
        J.update_state(push={"mtm": {"$each": [{"t": hhmm, "m": round(mtm)}], "$slice": -80}})
    return {"open": len(remaining), "closed": [c[0]["tid"] for c in closes]}


def a_eod(ctx, event):
    st = J.get_state()
    today = ctx["d"]
    dh = dhan_for(st)
    opened = st.get("open") or []
    prices = dh.ltp([l[0] for x in opened for l in x["legs"]]) if opened else dh.ltp([])
    unreal = sum(side * (prices.get(str(sid), fill) - fill) * qty for x in opened for sid, side, qty, fill, _, _ in x["legs"])
    inc = {}
    month = today[:7]
    rc = list(st.get("rc_months") or [])
    if month not in rc:
        inc["running"] = costs.MONTHLY_RUNNING_COSTS
        rc.append(month)
        st["running"] = float(st.get("running") or 0) + costs.MONTHLY_RUNNING_COSTS
    eq = equity(st)
    mark = eq + unreal
    peak = max(float(st.get("peak") or CAPITAL), mark)
    dd = mark / peak - 1
    margin_dhan = dh.margin([{"sid": l[0], "side": l[1], "qty": l[2], "px": prices.get(str(l[0])) or l[3]}
                             for x in opened for l in x["legs"]]) if opened else None
    open_docs = {t["_id"]: t for t in J.db().trades.find({"_id": {"$in": [x["tid"] for x in opened]}},
                                                          {"margin_model": 1, "sleeve": 1})} if opened else {}
    margin_model = sum(t.get("margin_model") or 0 for t in open_docs.values())
    closed_today = list(J.db().trades.find({"src": "paper", "exit_d": today}, {"sleeve": 1, "net": 1}))
    by_sleeve = {}
    for t in closed_today:
        by_sleeve[t["sleeve"]] = by_sleeve.get(t["sleeve"], 0) + t.get("net", 0)
    day_doc = {"equity": round(eq), "mark": round(mark), "unrealised": round(unreal), "realised_total": round(st.get("realised") or 0),
               "realised_today": round(sum(by_sleeve.values())), "by_sleeve": {k: round(v) for k, v in by_sleeve.items()},
               "running_total": round(st.get("running") or 0), "peak": round(peak), "dd": round(dd, 5),
               "open": [{"tid": x["tid"], "sl": x["sl"]} for x in opened], "margin_model": round(margin_model),
               "margin_dhan": margin_dhan, "spot": prices.get("spot"), "vix": prices.get("vix"),
               "mtm_path": st.get("mtm") or [], "errors": {k: v for k, v in (st.get("err") or {}).items() if k.startswith(today)}}
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
        fields.append({"name": "Running costs", "value": f"{N.fmt_rs(costs.MONTHLY_RUNNING_COSTS)} charged for {month}"})
    errs = sum(day_doc["errors"].values())
    if errs:
        fields.append({"name": "Errors today", "value": str(errs)})
    N.send("daily_report", f"📊 Daily report {today}", "", N.GREEN if day_doc["realised_today"] >= 0 else N.RED, fields)
    nxt = cal.next_trading_day(today)
    if nxt.isocalendar()[1] != cal.d(today).isocalendar()[1]:
        weekly_summary(ctx, today)
    return {"equity": eq, "mark": mark, "dd": dd}


def weekly_summary(ctx, today):
    monday = cal.d(today) - dt.timedelta(days=cal.d(today).weekday())
    rows = list(J.db().trades.find({"src": "paper", "exit_d": {"$gte": monday.isoformat(), "$lte": today}},
                                   {"sleeve": 1, "net": 1, "exit_reason": 1}))
    tot = sum(r.get("net", 0) for r in rows)
    by = {}
    for r in rows:
        by.setdefault(r["sleeve"], []).append(r.get("net", 0))
    lines = [f"**{k}**: {len(v)} trades, {N.fmt_rs(sum(v))}, win {sum(1 for x in v if x > 0)}/{len(v)}" for k, v in by.items()]
    N.send("daily_report", f"🗓️ Week {monday} → {today}", "\n".join(lines) or "No closed trades this week.",
           N.GREEN if tot >= 0 else N.RED, [{"name": "Week net", "value": N.fmt_rs(tot)}])


def a_reconcile(ctx, event):
    import reconcile
    return reconcile.run(ctx, event)


def a_status(ctx, event):
    st = J.get_state()
    valid, validity = dhan_for(st).token_info()
    info = {"paused": st.get("paused"), "flatten": st.get("flatten"), "equity": equity(st), "realised": st.get("realised"),
            "running": st.get("running"), "open": [x["tid"] for x in st.get("open") or []], "token_valid": valid,
            "token_validity": validity, "last_eod": st.get("last_eod"), "recon_pending": st.get("recon_pending"),
            "strategy_version": SV}
    N.send("system", "ℹ️ Status", "", N.BLUE, [{"name": k, "value": str(v)[:1000]} for k, v in info.items()])
    return info


def a_test(ctx, event):
    """Dry run: Dhan data + rules for today's 09:31 and 10:15 decisions. Writes nothing to trades/state."""
    res = {"entry": a_entry(ctx, event, dry=True), "monthly": a_monthly(ctx, event, dry=True)}
    lines = [f"{d['sleeve']}: {d['outcome']} — {d['why']}" for k in ("entry", "monthly") for d in res[k]["decisions"]]
    N.send("system", "🧪 TEST (dry run, nothing recorded)", "\n".join(lines)[:3900], N.GREY)
    return res


def a_init(ctx, event):
    J.ensure_indexes()
    st = J.get_state()
    if not st.get("dhan_token") and env("DHAN_ACCESS_TOKEN"):
        J.update_state(set_={"dhan_token": env("DHAN_ACCESS_TOKEN"), "token_upd": cal.ist_now().isoformat()})
    return {"indexes": "ok", "state": "ok"}


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
    "RENEW_TOKEN": a_renew_token, "RECONCILE": a_reconcile, "HEARTBEAT": a_heartbeat, "SNAPSHOT_0920": a_snapshot,
    "ENTRY_0931": a_entry, "MONTHLY_1015": a_monthly, "TICK": a_tick, "EOD": a_eod, "STATUS": a_status,
    "TEST": a_test, "INIT": a_init, "PAUSE": a_set_flag("paused", True, "⏸️ Paused: no new entries"),
    "RESUME": a_set_flag("paused", False, "▶️ Resumed"), "FLATTEN": a_set_flag("flatten", True, "🧯 Flatten requested (next TICK)"),
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
            hint = ("\nRenew: `python three_sleeve_bot/invoke.py SET_TOKEN --token <new token>`"
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
