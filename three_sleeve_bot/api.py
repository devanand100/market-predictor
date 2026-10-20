"""
Read-only rows for the trade-journal dashboard (dashboard/index.html), built from the MongoDB journal.

The dashboard expects two sources with exactly the columns of dashboard/sample/*.json:
  trades  id, sleeve, entry, exit, days_held, lots, detail, exit_reason, entry_px, exit_px, pnl, ret_pct, equity_after
  daily   date, trade_pnl, running_costs, trades_closed, equity, equity_mark
Used by dashboard/build_preview.py --mongo now, and by the planned dashboard Lambda (docs/DASHBOARD_PLAN.md).
Two indexed queries per page load; closed trades only.
"""
import datetime as dt

import journal as J

SLEEVE = {"L2": "Weekly strangle", "GAP": "Gap buy", "MON": "Monthly straddle"}


def _detail(t):
    i = t.get("in") or {}
    legs = {l["typ"]: l for l in t.get("legs") or []}
    if t["sleeve"] == "L2":
        return (f"Tranche {t.get('tranche', 0) + 1}: sell {legs.get('CE', {}).get('K')} CE / {legs.get('PE', {}).get('K')} PE"
                f" (straddle {i.get('S')}, width {i.get('w')})")
    if t["sleeve"] == "GAP":
        typ = next(iter(legs), "")
        return f"Buy ATM {legs.get(typ, {}).get('K')} {typ} (gap {100 * (i.get('gap') or 0):+.2f}%, VIX {i.get('vix_0920')})"
    return f"Sell {legs.get('CE', {}).get('K')} straddle, exp {t.get('expiry')}, VRP {i.get('vrp')}"


def trades_rows(src="paper"):
    out = []
    for t in J.db().trades.find({"src": src, "status": "closed"}).sort([("exit_d", 1), ("entry_d", 1)]):
        e, x = t.get("entry_d"), t.get("exit_d")
        out.append({"id": t["_id"], "sleeve": SLEEVE.get(t["sleeve"], t["sleeve"]), "entry": e, "exit": x,
                    "days_held": (dt.date.fromisoformat(x) - dt.date.fromisoformat(e)).days if e and x else None,
                    "lots": t.get("lots"), "detail": _detail(t), "exit_reason": t.get("exit_reason"),
                    "entry_px": t.get("px_in"), "exit_px": t.get("px_out"), "pnl": round(t.get("net") or 0),
                    "ret_pct": t.get("ret_pct"), "equity_after": t.get("equity_after")})
    return out


def daily_rows(src="paper"):
    if src != "paper":
        return []                      # research daily equity comes from the backtest export, not the journal
    return [{"date": d["d"], "trade_pnl": d.get("realised_today", 0), "running_costs": d.get("running_today", 0),
             "trades_closed": d.get("closed_n", 0), "equity": d.get("equity"), "equity_mark": d.get("mark")}
            for d in J.db().days.find({}, {"d": 1, "realised_today": 1, "running_today": 1, "closed_n": 1,
                                           "equity": 1, "mark": 1}).sort("d", 1)]
