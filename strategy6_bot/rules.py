"""
Strategy 6 rules (docs/STRATEGY_6_REALTRADE.md), pure functions with no I/O.

The research implementation is research_realtrade.Weekly with the sleeves of
research_realtrade_portfolio.sleeves(0.45, 0.45, 0.05); tests/test_parity.py checks these functions
against the research trades. "VIX" is always the PREVIOUS session's India VIX close.

  S2   VIX >= 15, 2 sessions before the weekly expiry, 09:30: sell CE/PE at ATM +/- 1.25 x ATM straddle.
       Stop when CE+PE >= 2.0 x entry (loss = 1.0 x credit), else 15:15 on expiry day. 55% of equity as margin.
  E0   VIX >= 15, expiry day, 09:30: sell CE/PE at ATM +/- 0.5 x straddle. Stop when CE+PE >= 1.5 x entry
       (loss = 0.5 x credit), else 15:15. 55% of equity as margin (expiry-day margin 11%).
  GAP  VIX <= 13, 4 sessions before expiry, 09:30: buy the ATM CE if spot > previous close, else ATM PE.
       Limit sell at 2 x fill, else 15:15 on expiry day. 5% of equity in premium.
  Total margin (shorts) + premium (longs) is capped at 90% of equity.
"""
from costs import LOT, slip

STEP = 50
MARGIN_PCT = 0.09             # naked short margin per lot, fraction of notional (research_realtrade.MARGIN_PCT)
EXPIRY_ELM = 0.02             # SEBI extra exposure margin on expiry day for short options (from 2024-11-20)
MAX_UTIL = 0.90
WINDOW_MAX = 450              # research never sells a strike more than 450 from ATM (the trade is skipped)
VIX_HI, VIX_LO = 15.0, 13.0
# Research final weights are 0.45 / 0.45 / 0.05 (sized for Rs 10L). At Rs 3L one short lot needs 0.09 x spot x 65
# (~Rs 1.31L at Nifty 22,400), so 45% of equity fits one lot only while Nifty < ~23,077. 0.55 keeps one lot tradable
# up to Nifty ~28,200; on the research replay at Rs 3L it gives 31.3% CAGR / -13.2% DD / PF 1.76 (0.45: 28.8 / -12.5 / 1.80).
W_S2, W_E0, F_GAP = 0.55, 0.55, 0.05
S2_DTE, S2_MULT, S2_STOP = 2, 1.25, 1.0
E0_MULT, E0_STOP = 0.5, 0.5
GAP_DTE, GAP_TP = 4, 1.0
EXIT_HHMM = "15:16"           # first 1-minute tick after the 15:15 bar
MONTHLY_RUNNING_COSTS = 590.0  # Rs/month: Dhan Data API incl. GST. Lambda, MongoDB free tier and Discord cost nothing.


def params():
    return {k: v for k, v in globals().items() if k.isupper()}


def atm(spot):
    return int(round(spot / STEP) * STEP)       # Python round, as engine.atm_strike


def rnd(x):
    return max(50, int(round(x / 50)) * 50)       # research_realtrade_grid.rnd


# ---------------------------------------------------------------- fills (research conventions)
def sell_fill(px, urgent=False):
    return px - slip(px, urgent)


def buy_fill(px, urgent=False):
    return px + slip(px, urgent)


def close_long_fill(px, urgent=False):
    return max(0.05, px - slip(px, urgent))


def close_short_fill(px, urgent=False):
    return px + slip(px, urgent)


# ---------------------------------------------------------------- calendar gates
def to_expiry(i, n):
    """Sessions left until the weekly expiry (0 = expiry day)."""
    return n - 1 - i


def decide(sleeve, i, n, vix_prev):
    """-> (due, reason). vix_prev = previous session's VIX close."""
    left = to_expiry(i, n)
    want = {"S2": S2_DTE, "E0": 0, "GAP": GAP_DTE}[sleeve]
    if left != want:
        return False, f"not the entry day ({left} session(s) to expiry, needs {want})"
    if vix_prev is None:
        return False, "previous VIX close unavailable"
    if sleeve in ("S2", "E0") and vix_prev < VIX_HI:
        return False, f"VIX {vix_prev:.2f} < {VIX_HI:g}"
    if sleeve == "GAP" and vix_prev > VIX_LO:
        return False, f"VIX {vix_prev:.2f} > {VIX_LO:g}"
    return True, f"VIX {vix_prev:.2f} {'≥ ' + format(VIX_HI, 'g') if sleeve != 'GAP' else '≤ ' + format(VIX_LO, 'g')}"


# ---------------------------------------------------------------- strikes
def strangle(spot, atm_ce, atm_pe, mult):
    """-> dict(K, S, w, ce, pe) or None when a strike would be more than WINDOW_MAX from ATM (research skips)."""
    K = atm(spot)
    S = atm_ce + atm_pe
    w = rnd(mult * S)
    if w > WINDOW_MAX:
        return None
    return {"K": K, "S": S, "w": w, "ce": K + w, "pe": K - w}


def gap_side(spot_0930, prev_close):
    """-> (opt_type or None, gap)."""
    gap = spot_0930 / prev_close - 1 if prev_close else 0.0
    if gap == 0:
        return None, gap
    return ("CE" if gap > 0 else "PE"), gap


# ---------------------------------------------------------------- sizing
def margin_pct(expiry_day):
    return MARGIN_PCT + (EXPIRY_ELM if expiry_day else 0.0)


def short_lots(equity, spot, weight, expiry_day, used):
    """Whole lots for a naked strangle: weight x equity of margin, never above MAX_UTIL x equity in total."""
    cap = margin_pct(expiry_day) * spot * LOT
    lots = int(weight * equity // cap)
    return max(0, min(lots, int((MAX_UTIL * equity - used) // cap))), cap


def gap_lots(equity, ltp, used):
    """Premium per lot is priced at the pre-slippage LTP, as research_realtrade.Weekly does."""
    cap = ltp * LOT
    if cap <= 0:
        return 0, cap
    lots = int(F_GAP * equity // cap)
    return max(0, min(lots, int((MAX_UTIL * equity - used) // cap))), cap


# ---------------------------------------------------------------- exits
def stop_level(entry_sum, mult):
    """CE+PE price at which the strangle has lost mult x its credit."""
    return (1 + mult) * entry_sum


def stop_hit(entry_sum, now_sum, mult):
    return now_sum >= stop_level(entry_sum, mult)


def gap_target(fill):
    return (1 + GAP_TP) * fill


def gap_target_hit(target, ltp):
    """Research fills the resting limit when the 1-minute high trades >= target + 0.05."""
    return ltp >= target + 0.05
