"""
Next-morning reconciliation: replay yesterday with the research rules on Dhan 1-minute bars and compare
with what the paper bot did (paper trades in Mongo). One `recon` document + one Discord embed per day.

Compared per paper trade active on the day:
  entries      strikes (L2 width / gap ATM) and fills: paper LTP fill vs research bar-close fill
  L2 tranches  research stop minute on 5-min marks (with the paper entry fills) vs the paper exit
  gap buy      research target on 1-min closes vs the paper 5-min check; expiry 15:15 exit fill
  monthly      entry/exit fill vs the research 09:15 60-min bar close (fills ~10:15)
  decisions    research gap/VIX decision vs the paper decision (missing / extra entries)
  spread       paper bid/ask half-spread at entry vs the slip model
If Dhan has no data for the day yet, the day stays in state.recon_pending and is retried next morning.
"""
import costs
import journal as J
import market_calendar as cal
import notify as N
import replay as RP
import rules as R
from dhan import Dhan, NIFTY_ID, VIX_ID

OFFSETS = range(-10, 11)


def hm(m):
    t = 9 * 60 + 15 + m
    return f"{t // 60:02d}:{t % 60:02d}"


class DhanDaySource:
    """Price source for replay.py built from one day of Dhan rolling-option 1-min bars (weekly, code 1)."""

    def __init__(self, dh, day):
        self.day = day
        self.px_by = {}       # (strike, typ) -> {HH:MM: close}
        self.spot_by = {}     # HH:MM -> option-data spot
        for typ in ("CE", "PE"):
            for off in OFFSETS:
                for t, (k, c, s) in dh.rolling_option("WEEK", 1, off, typ, day).items():
                    self.px_by.setdefault((k, typ), {})[t] = c
                    self.spot_by.setdefault(t, s)
        self.index = dh.intraday(NIFTY_ID, "IDX_I", "INDEX", day)
        self.vix = dh.intraday(VIX_ID, "IDX_I", "INDEX", day)

    def has_data(self):
        return bool(self.px_by) and bool(self.spot_by)

    def spot(self, day, m):
        for mm in range(m, max(-1, m - 6), -1):
            v = self.spot_by.get(hm(mm)) or self.index.get(hm(mm))
            if v:
                return v
        return None

    def px(self, day, strike, typ, m):
        series = self.px_by.get((int(strike), typ)) or {}
        for mm in range(m, max(-1, m - 6), -1):     # engine: forward-fill short gaps (<= 5 min)
            if hm(mm) in series:
                return series[hm(mm)]
        s = self.spot(day, m) or 0.0
        return max(0.05, (s - strike) if typ == "CE" else (strike - s))   # engine: intrinsic fallback

    def vix_at(self, m):
        return self.vix.get(hm(m))


def monthly_bar(dh, day, expiry, monthly_list, strike):
    """Research fill basis for the monthly straddle: the 09:15 60-min bar close of the contract."""
    later = [e for e in monthly_list if e >= day]
    code = later.index(expiry) + 1 if expiry in later else None
    if code not in (1, 2):
        return None
    out = {}
    for typ in ("CE", "PE"):
        for off in range(-3, 4):
            for t, (k, c, s) in dh.rolling_option("MONTH", code, off, typ, day, interval=60).items():
                if k == strike and t == "09:15":
                    out[typ] = c
    return out if len(out) == 2 else None


def diff(paper, replay):
    return None if paper is None or replay is None else round(paper - replay, 2)


def reconcile_day(dh, day, st):
    trades = list(J.db().trades.find({"src": "paper", "entry_d": {"$lte": day},
                                      "$or": [{"status": "open"}, {"exit_d": {"$gte": day}}]}))
    if not trades and not J.db().decisions.find_one({"d": day, "sleeve": "GAP"}, {"_id": 1}):
        return {"items": [], "notes": [], "summary": {"trades": 0, "mismatches": 0, "mean_abs_fill_diff": None},
                "dhan_calls": 0}
    src = DhanDaySource(dh, day)
    if not src.has_data():
        return None
    marks_all = list(range(0, 376, 5))
    items, notes = [], []
    for t in trades:
        legs = {l["typ"]: l for l in t["legs"]}
        rec = {"tid": t["_id"], "sleeve": t["sleeve"], "key": t["key"]}
        entry_today, exit_today = t["entry_d"] == day, t.get("exit_d") == day
        if t["sleeve"] == "L2":
            ce, pe = legs["CE"], legs["PE"]
            if entry_today:
                o = RP.l2_open_tranche(src, day, RP.ENTRY_MIN, t["qty"])
                rec.update(w_paper=ce["K"] - t["in"].get("K", ce["K"]), w_replay=o["w"],
                           strikes_match=(o["ce"], o["pe"]) == (ce["K"], pe["K"]),
                           ce_fill_diff=diff(ce["fill"], o["ce_fill"]), pe_fill_diff=diff(pe["fill"], o["pe_fill"]))
            first = RP.ENTRY_MIN if entry_today else 0
            last = RP.EXIT_MIN if t["expiry"] == day else 375
            marks = [m for m in marks_all if first <= m <= last]
            tr = {"ce": ce["K"], "pe": pe["K"], "ce_fill": ce["fill"], "pe_fill": pe["fill"]}
            sm = RP.l2_stop_minute(src, tr, day, marks)
            rec["replay_stop"] = hm(sm) if sm is not None else None
            rec["paper_exit"] = t.get("exit_reason") if exit_today else None
            if (sm is not None) != (exit_today and str(t.get("exit_reason", "")).startswith("stop")):
                notes.append(f"{t['_id']}: stop mismatch (replay {rec['replay_stop']}, paper {rec['paper_exit']})")
        elif t["sleeve"] == "GAP":
            l = legs.get("CE") or legs.get("PE")
            if entry_today:
                rec["fill_diff"] = diff(l["fill"], R.buy_fill(src.px(day, l["K"], l["typ"], RP.ENTRY_MIN)))
            g = {"K": l["K"], "typ": l["typ"], "fill": l["fill"]}
            m, reason, fill = RP.gap_exit_minute(src, g, day, RP.ENTRY_MIN if entry_today else 0,
                                                 RP.EXIT_MIN if t["expiry"] == day else 375, t["expiry"] == day)
            rec.update(replay_exit=reason, replay_exit_t=hm(m) if m is not None else None,
                       paper_exit=t.get("exit_reason") if exit_today else None)
            if exit_today and fill is not None:
                pf = next((x["fill"] for x in t.get("exit_legs", [])), None)
                rec["exit_fill_diff"] = diff(pf, fill)
            if (reason is not None) != exit_today:
                notes.append(f"{t['_id']}: exit mismatch (replay {reason} {rec['replay_exit_t']}, paper {rec['paper_exit']})")
        elif t["sleeve"] == "MON" and (entry_today or exit_today):
            mon = cal.monthly_expiries(st.get("exp_seen") or [])
            bar = monthly_bar(dh, day, t["expiry"], mon, legs["CE"]["K"])
            if bar:
                if entry_today:
                    rec["entry_fill_diff"] = diff(legs["CE"]["fill"] + legs["PE"]["fill"],
                                                  R.sell_fill(bar["CE"]) + R.sell_fill(bar["PE"]))
                if exit_today:
                    xs = {x["typ"]: x["fill"] for x in t.get("exit_legs", [])}
                    rec["exit_fill_diff"] = diff(xs.get("CE", 0) + xs.get("PE", 0),
                                                 R.close_short_fill(bar["CE"]) + R.close_short_fill(bar["PE"]))
            else:
                rec["note"] = "monthly 60-min bar unavailable"
        if entry_today:
            half = [((l.get("ask") or 0) - (l.get("bid") or 0)) / 2 for l in t["legs"] if l.get("ask") and l.get("bid")]
            slips = [costs.slip(l["ltp"]) for l in t["legs"] if l.get("ltp")]
            if half and slips:
                rec["half_spread"], rec["slip_model"] = round(sum(half) / len(half), 2), round(sum(slips) / len(slips), 2)
        items.append(rec)
    # research decision for the gap buy vs the paper decision
    gd = J.db().decisions.find_one({"d": day, "sleeve": "GAP"})
    if gd:
        vix = src.vix_at(5)                       # 09:20 bar
        spot = src.spot(day, RP.ENTRY_MIN)
        pc = (gd.get("in") or {}).get("prev_close")
        take, typ, gap, why = R.gap_decision(gd["in"].get("cycle_n", 0), gd["in"].get("cycle_i", 0), spot or 0, pc, vix)
        rec = {"key": f"GAP|{day}|decision", "paper": gd["outcome"], "replay": "enter" if take else "skip",
               "replay_why": why, "vix_paper": gd["in"].get("vix_0920"), "vix_replay": vix}
        items.append(rec)
        if rec["paper"] != rec["replay"]:
            notes.append(f"gap decision differs: paper {gd['outcome']} / replay {rec['replay']} ({why})")
    fill_diffs = [abs(v) for r in items for k, v in r.items() if k.endswith("fill_diff") and v is not None]
    summary = {"trades": len(trades), "mismatches": len(notes),
               "mean_abs_fill_diff": round(sum(fill_diffs) / len(fill_diffs), 2) if fill_diffs else None}
    return {"items": items, "notes": notes, "summary": summary, "dhan_calls": dh.calls}


def run(ctx, event):
    st = J.get_state({"dhan_token": 1, "recon_pending": 1, "exp_seen": 1})
    import os
    dh = Dhan(os.environ.get("DHAN_CLIENT_ID", ""), st.get("dhan_token") or os.environ.get("DHAN_ACCESS_TOKEN", ""),
              min_gap_s=0.35)
    target = event.get("recon_date") or cal.prev_trading_day(ctx["d"]).isoformat()
    days = sorted(set((st.get("recon_pending") or []) + [target]))[-5:]
    done, pending, out = [], [], {}
    for day in days:
        if J.db().recon.find_one({"d": day}, {"_id": 1}) and day != event.get("recon_date"):
            done.append(day)
            continue
        res = reconcile_day(dh, day, st)
        if res is None:
            pending.append(day)
            continue
        J.upsert_recon(ctx, day, res)
        done.append(day)
        out[day] = res["summary"]
        s = res["summary"]
        if not s["trades"] and not res["items"]:
            continue                       # nothing traded or decided that day: no message
        N.send("reconciliation", f"Reconciliation {day}",
               ("\n".join(f"• {n}" for n in res["notes"]) or "Paper matches the research replay.")[:3500],
               N.ORANGE if res["notes"] else N.GREEN,
               [{"name": "Trades checked", "value": str(s["trades"])},
                {"name": "Mismatches", "value": str(s["mismatches"])},
                {"name": "Mean |fill diff| (pts)", "value": str(s["mean_abs_fill_diff"])}])
    J.update_state(set_={"recon_pending": pending})
    return {"reconciled": out, "pending": pending}
