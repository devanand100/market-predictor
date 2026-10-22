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
import math
import statistics

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
# Strategy 7 book (docs/STRATEGY_7_REALTRADE.md): Strategy 6 at the research weights + the monthly straddle,
# sized on Dhan-level margin (Dhan quoted Rs 1.72L per strangle lot on 2026-10-22 = 11.6% of notional)
S7_MARGIN_PCT = 0.116
S7_W_S2, S7_W_E0, S7_F_GAP, S7_W_MON = 0.45, 0.45, 0.05, 0.20
VRP_MIN = 2.0
MON_ENTRY_DTE, MON_ENTRY_MIN_DTE, MON_EXIT_DTE = 45, 41, 21
EXIT_HHMM = "15:16"
# daily-report health alerts (docs/STRATEGY_6_REALTRADE.md section 7)
ALERT_PF_TRADES, ALERT_PF_MIN_TRADES, ALERT_PF_MIN = 20, 10, 1.0   # rolling PF of the last 20 closed trades < 1.0
ALERT_DD = -0.15                                                   # mark-to-market drawdown beyond -15%
ALERT_VRP_DAYS = 20                                                # 20-session average VIX - RV20 below 0
ALERT_SPREAD_X, ALERT_SPREAD_TRADES = 2.0, 20                     # median spread cost > 2x modelled slippage
# kill switch, NOTIFY ONLY in paper (research_killswitch.py: best of 8 rules; never fires on real 2021-26, caps the
# 2020 COVID drawdown near -19% on synthetic prices): would pause new entries when drawdown <= -15%, would resume
# when the last 20 closed trades have PF >= 1.3
KILL_DD, KILL_RESUME_PF, KILL_RESUME_TRADES = -0.15, 1.3, 20
# Strategy 7 book: -16%. Its real 2021-26 daily MTM worst is -14.97%, so -15% has no headroom; -16% still caps both
# synthetic COVID drawdowns at -19% (-17%/-18% let the base case reach -21.7%). Chosen 2026-10-23.
KILL_DD_S7 = -0.16           # first 1-minute tick after the 15:15 bar
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
def margin_pct(expiry_day, base=None):
    return (MARGIN_PCT if base is None else base) + (EXPIRY_ELM if expiry_day else 0.0)


def short_lots(equity, spot, weight, expiry_day, used, base=None):
    """Whole lots for a naked strangle: weight x equity of margin, never above MAX_UTIL x equity in total."""
    cap = margin_pct(expiry_day, base) * spot * LOT
    lots = int(weight * equity // cap)
    return max(0, min(lots, int((MAX_UTIL * equity - used) // cap))), cap


def gap_lots(equity, ltp, used, frac=None):
    """Premium per lot is priced at the pre-slippage LTP, as research_realtrade.Weekly does."""
    cap = ltp * LOT
    if cap <= 0:
        return 0, cap
    lots = int((F_GAP if frac is None else frac) * equity // cap)
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


# ---------------------------------------------------------------- monthly straddle (Strategy 7 book)
# Same rules as three_sleeve_bot/rules.py (parity-tested there against research_monthly_straddle.py).
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


def mon_lots(equity, spot, used, base=S7_MARGIN_PCT, weight=S7_W_MON):
    """research_realtrade_s7.MonthlyReal: weight x equity of margin, never above MAX_UTIL x equity in total."""
    cap = base * spot * LOT
    return max(0, min(int(weight * equity // cap), int((MAX_UTIL * equity - used) // cap))), cap


# ---------------------------------------------------------------- health alerts
def profit_factor(nets):
    w = sum(x for x in nets if x > 0)
    l = -sum(x for x in nets if x < 0)
    return w / l if l > 0 else (float("inf") if w > 0 else None)


def vrp_series(vix_closes, nifty_closes):
    """[(date, VIX close - RV20 at that date)] for dates present in both lists (oldest first)."""
    nd = [d for d, _ in nifty_closes]
    nc = [c for _, c in nifty_closes]
    vix = dict(vix_closes)
    out = []
    for i, d in enumerate(nd):
        rv = rv20(nc[:i + 1])
        if rv is not None and d in vix:
            out.append((d, vix[d] - rv))
    return out


def spread_ratio(leg):
    """Cost of crossing the spread at entry / modelled slippage for one leg (None without a usable quote)."""
    ltp, bid, ask = leg.get("ltp") or 0, leg.get("bid") or 0, leg.get("ask") or 0
    if ltp <= 0 or bid <= 0 or ask <= 0:
        return None
    cost = (ltp - bid) if leg["side"] < 0 else (ask - ltp)
    return max(cost, 0.0) / slip(ltp)


def kill_switch(active, dd, last_nets, kill_dd=KILL_DD):
    """-> (active_now, event, reason). event in None / 'fire' / 'resume'. NOTIFY ONLY: the bot keeps trading."""
    pf = profit_factor(last_nets[-KILL_RESUME_TRADES:]) if len(last_nets) >= KILL_RESUME_TRADES else None
    pf_txt = f"{pf:.2f}" if pf is not None else f"n/a ({len(last_nets)} trades)"
    if not active:
        if dd <= kill_dd:
            return True, "fire", f"drawdown {dd:.1%} ≤ {kill_dd:.0%}"
        return False, None, f"inactive (drawdown {dd:.1%}, fires at {kill_dd:.0%})"
    if pf is not None and pf >= KILL_RESUME_PF:
        return False, "resume", f"last {KILL_RESUME_TRADES} trades PF {pf_txt} ≥ {KILL_RESUME_PF:g}"
    return True, None, f"would stay paused: last {KILL_RESUME_TRADES} trades PF {pf_txt} < {KILL_RESUME_PF:g} (drawdown {dd:.1%})"
