"""
Strategy 5 rules (docs/PORTFOLIO_STRATEGY.md), pure functions with no I/O.

Shared by the live Lambda (prices = Dhan LTP) and by replay.py (prices = 1-min bar closes), so live
and backtest decisions come from one implementation. Sources in the research code:
  L2   research_institutional.py run(kind="L2")      GAP  final_gap_buy.py + research_regime_switch.gap_pnl
  MON  research_monthly_straddle.py + research_monthly_straddle_vix.py       sizing final_portfolio_strategy.py
"""
import math
import statistics

from costs import LOT, slip

STEP = 50
MARGIN_PCT = 0.09
W_L2, W_GAP, W_MON = 0.30, 0.03, 0.20
VIX_MAX = 15.0
VRP_MIN = 2.0
WINDOW_MAX = 450              # strangle width cap (keeps strikes inside the researched +/-500 window)
L2_STOP_MULT = 1.5            # tranche exits when its loss >= 1.5 x its credit
GAP_TP = 1.0                  # gap buy exits when the price is >= (1 + GAP_TP) x fill
MON_ENTRY_DTE, MON_ENTRY_MIN_DTE, MON_EXIT_DTE = 45, 41, 21
MIN_SHORT_PX = 1.0            # research skips a strangle whose short leg fills below Rs 1


def params():
    return {k: v for k, v in globals().items() if k.isupper()}


def atm(spot):
    return int(round(spot / STEP) * STEP)       # Python round, as engine.atm_strike


# ---------------------------------------------------------------- fills (research conventions)
def sell_fill(px, urgent=False):
    return px - slip(px, urgent)


def buy_fill(px, urgent=False):
    return px + slip(px, urgent)


def close_long_fill(px, urgent=False):
    return max(0.05, px - slip(px, urgent))


def close_short_fill(px, urgent=False):
    return px + slip(px, urgent)


# ---------------------------------------------------------------- L2 weekly staggered strangle
def l2_enters_today(i, n):
    """Tranche on cycle day 0, and on days 1 and 2 unless that day is the expiry day."""
    return i == 0 or (0 < i <= 2 and i < n - 1)


def l2_total_lots(equity, spot):
    n = int(W_L2 * equity // (MARGIN_PCT * spot * LOT))
    return n if n >= 3 else 0


def l2_width(S):
    return min(WINDOW_MAX, max(50, int(round(S / STEP)) * STEP))


def l2_strikes(spot, atm_ce, atm_pe):
    K = atm(spot)
    S = atm_ce + atm_pe
    w = l2_width(S)
    return {"K": K, "S": S, "w": w, "ce": K + w, "pe": K - w}


def l2_stop_hit(entry_ce, entry_pe, ce, pe):
    """Tranche P&L (per unit, short both) <= -1.5 x tranche credit."""
    pnl = (entry_ce - ce) + (entry_pe - pe)
    return pnl <= -L2_STOP_MULT * (entry_ce + entry_pe)


# ---------------------------------------------------------------- gap buy
def gap_decision(cycle_n, cycle_i, spot_0930, prev_close, vix_0920):
    """-> (take, opt_type, gap, reason)"""
    gap = spot_0930 / prev_close - 1 if prev_close else 0.0
    if cycle_n < 5:
        return False, None, gap, f"short week ({cycle_n} sessions): gap buy trades full weeks only"
    if cycle_i != cycle_n - 5:
        return False, None, gap, f"not the entry day (cycle day {cycle_i} of {cycle_n})"
    if vix_0920 is None:
        return False, None, gap, "VIX 09:20 unavailable"
    if vix_0920 > VIX_MAX:
        return False, None, gap, f"VIX 09:20 {vix_0920:.2f} > {VIX_MAX:g}"
    if gap == 0:
        return False, None, gap, "gap exactly 0"
    typ = "CE" if gap > 0 else "PE"
    return True, typ, gap, f"gap {gap:+.2%} → buy ATM {typ}, VIX {vix_0920:.2f} ≤ {VIX_MAX:g}"


def gap_lots(equity, fill):
    per_lot = fill * LOT
    lots = int(W_GAP * equity // per_lot)
    if lots == 0 and per_lot <= 2 * W_GAP * equity:
        lots = 1
    return lots


def gap_target_hit(fill, px):
    return px - fill >= GAP_TP * fill


# ---------------------------------------------------------------- monthly straddle
def rv20(closes):
    """Annualised % stdev of the last 20 daily log returns (needs >= 21 closes, oldest first)."""
    c = closes[-21:]
    if len(c) < 21:
        return None
    r = [math.log(b / a) for a, b in zip(c[:-1], c[1:])]
    return statistics.stdev(r) * math.sqrt(252) * 100


def vrp(vix_prev_close, closes_upto_prev):
    rv = rv20(closes_upto_prev)
    return (None, rv) if rv is None or vix_prev_close is None else (vix_prev_close - rv, rv)


def mon_entry_due(expiry_dte, prev_session_dte):
    """First session at <= 45 DTE, and only if it is still >= 41 DTE."""
    return MON_ENTRY_MIN_DTE <= expiry_dte <= MON_ENTRY_DTE < prev_session_dte


def mon_exit_due(expiry_dte):
    return expiry_dte <= MON_EXIT_DTE


def forward(chain, spot):
    """chain: {strike: (ce, pe)} with both prices > 0. Median of K + C - P over the 3 strikes nearest spot."""
    both = sorted((k for k, (c, p) in chain.items() if c and p), key=lambda k: abs(k - spot))[:3]
    if not both:
        return None
    return float(statistics.median([k + chain[k][0] - chain[k][1] for k in both]))


def mon_strike(chain, spot):
    F = forward(chain, spot)
    if F is None:
        return None, None
    both = [k for k, (c, p) in chain.items() if c and p]
    return min(both, key=lambda k: abs(k - F)), F


def mon_lots(equity, spot):
    return int(W_MON * equity // (MARGIN_PCT * spot * LOT))


# ---------------------------------------------------------------- equity
def margin_model(spot, lots):
    return MARGIN_PCT * spot * LOT * lots
