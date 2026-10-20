"""
Replay of the rules on 1-minute bar closes (the research fill model), using rules.py.

A price source provides, for one weekly contract:
    spot(date, m)                  index level at minute m (m = minutes after 09:15; 15 = 09:30, 360 = 15:15)
    px(date, strike, typ, m)       option close at minute m
Used by:
  * tests/test_parity.py  - engine 1-min data, must reproduce research_institutional L2 and final_gap_buy
  * reconcile.py          - one day of Dhan 1-min data, compared with the paper trades of that day
"""
import costs
import rules

ENTRY_MIN, EXIT_MIN, CHECK_EVERY = 15, 360, 5


def l2_minutes(window):
    """research_institutional.minutes_path: 5-minute marks from 09:30 day 0 to 15:15 on expiry."""
    for i, day in enumerate(window):
        a = ENTRY_MIN if i == 0 else 0
        b = EXIT_MIN if i == len(window) - 1 else 375
        for m in range(a, b + 1, CHECK_EVERY):
            yield i, day, m
    yield len(window) - 1, window[-1], EXIT_MIN


def l2_open_tranche(src, day, m, qty):
    spot = src.spot(day, m)
    K = rules.atm(spot)
    s = rules.l2_strikes(spot, src.px(day, K, "CE", m), src.px(day, K, "PE", m))
    ce_px, pe_px = src.px(day, s["ce"], "CE", m), src.px(day, s["pe"], "PE", m)
    t = {"day": day, "m": m, "spot": spot, **s, "qty": qty,
         "ce_fill": rules.sell_fill(ce_px), "pe_fill": rules.sell_fill(pe_px), "open": True}
    t["cost"] = (costs.option_fill_cost(t["ce_fill"], qty, False, day) +
                 costs.option_fill_cost(t["pe_fill"], qty, False, day))
    t["credit"] = (t["ce_fill"] + t["pe_fill"]) * qty
    return t


def l2_close_tranche(src, t, day, m, urgent, reason):
    ce = rules.close_short_fill(src.px(day, t["ce"], "CE", m), urgent)
    pe = rules.close_short_fill(src.px(day, t["pe"], "PE", m), urgent)
    t.update(open=False, exit_day=day, exit_m=m, reason=reason, ce_exit=ce, pe_exit=pe)
    t["cost"] += costs.option_fill_cost(ce, t["qty"], True, day) + costs.option_fill_cost(pe, t["qty"], True, day)
    t["pnl"] = ((t["ce_fill"] - ce) + (t["pe_fill"] - pe)) * t["qty"] - t["cost"]


def replay_l2_cycle(src, window, qty):
    """Whole L2 cycle with `qty` units per tranche. -> (tranches, net P&L) or None if skipped."""
    tranches = [l2_open_tranche(src, window[0], ENTRY_MIN, qty)]
    if min(tranches[0]["ce_fill"], tranches[0]["pe_fill"]) < rules.MIN_SHORT_PX:
        return None
    n = len(window)
    for i, day, m in l2_minutes(window):
        if m == ENTRY_MIN and 0 < i <= 2 and rules.l2_enters_today(i, n) and len(tranches) < i + 1:
            tranches.append(l2_open_tranche(src, day, m, qty))
        if day == window[-1] and m >= EXIT_MIN:
            for t in tranches:
                if t["open"]:
                    l2_close_tranche(src, t, day, m, False, "time")
            break
        for t in tranches:
            if t["open"] and rules.l2_stop_hit(t["ce_fill"], t["pe_fill"], src.px(day, t["ce"], "CE", m),
                                               src.px(day, t["pe"], "PE", m)):
                l2_close_tranche(src, t, day, m, True, "stop")
        if not any(t["open"] for t in tranches) and len(tranches) >= min(3, n - 1):
            break
    for t in tranches:
        if t["open"]:
            l2_close_tranche(src, t, window[-1], EXIT_MIN, False, "time")
    return tranches, sum(t["pnl"] for t in tranches)


def l2_stop_minute(src, t, day, marks):
    """First 5-minute mark of `day` at which an open tranche (paper fills) hits its stop, else None."""
    for m in marks:
        if rules.l2_stop_hit(t["ce_fill"], t["pe_fill"], src.px(day, t["ce"], "CE", m), src.px(day, t["pe"], "PE", m)):
            return m
    return None


def gap_open(src, day, prev_close, vix_0920, cycle_n, cycle_i):
    spot = src.spot(day, ENTRY_MIN)
    take, typ, gap, why = rules.gap_decision(cycle_n, cycle_i, spot, prev_close, vix_0920)
    if not take:
        return {"take": False, "why": why, "gap": gap}
    K = rules.atm(spot)
    fill = rules.buy_fill(src.px(day, K, typ, ENTRY_MIN))
    return {"take": True, "why": why, "gap": gap, "K": K, "typ": typ, "fill": fill, "spot": spot}


def gap_exit_minute(src, g, day, first_m=ENTRY_MIN, last_m=EXIT_MIN, is_expiry=False):
    """research run_cycle: every 1-min close from first_m; target when close >= 2 x fill. -> (m, reason, fill)"""
    for m in range(first_m, last_m + 1):
        px = src.px(day, g["K"], g["typ"], m)
        if rules.gap_target_hit(g["fill"], px):
            return m, "target", rules.close_long_fill(px)
    if is_expiry:
        px = src.px(day, g["K"], g["typ"], EXIT_MIN)
        return EXIT_MIN, "time", rules.close_long_fill(px)
    return None, None, None


def replay_gap_cycle(src, w5, prev_close, vix_0920):
    """Full-week gap buy over the 5 sessions w5 (entry w5[0], expiry w5[-1]). -> dict or None."""
    g = gap_open(src, w5[0], prev_close, vix_0920, 5, 0)
    if not g["take"]:
        return None
    for j, day in enumerate(w5):
        last = EXIT_MIN if j == len(w5) - 1 else 375
        m, reason, fill = gap_exit_minute(src, g, day, ENTRY_MIN if j == 0 else 0, last, j == len(w5) - 1)
        if m is not None:
            g.update(exit_day=day, exit_m=m, reason=reason, sell=fill)
            return g
    return g
