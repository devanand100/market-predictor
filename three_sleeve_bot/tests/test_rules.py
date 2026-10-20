"""Unit tests of rules.py against the research definitions."""
import math

import numpy as np
import pandas as pd
import pytest

import market_calendar as cal
import rules as R


def test_l2_tranche_days_match_research():
    # research: tranche 0 always; days 1 and 2 only if not the expiry day (i < len(window) - 1)
    for n in range(1, 8):
        got = [i for i in range(n) if R.l2_enters_today(i, n)]
        want = [0] + [i for i in (1, 2) if i < n - 1]
        assert got == want, n


def test_l2_width_cap_and_floor():
    assert R.l2_width(10) == 50
    assert R.l2_width(312) == 300
    assert R.l2_width(1200) == 450
    assert R.l2_strikes(25012, 160, 150) == {"K": 25000, "S": 310, "w": 300, "ce": 25300, "pe": 24700}


def test_l2_stop_equals_research_condition():
    # research: sum(qty*(px - entry)) <= -1.5 * credit  with qty < 0  <=>  ce+pe >= 2.5*(entry sum)
    assert not R.l2_stop_hit(40, 35, 93.7, 93.7)
    assert R.l2_stop_hit(40, 35, 100, 87.5)


def test_l2_lots_need_three():
    assert R.l2_total_lots(1_000_000, 25_000) == 0          # 2 lots < 3
    assert R.l2_total_lots(1_500_000, 25_000) == 3


def test_gap_decision_and_lots():
    assert R.gap_decision(5, 0, 25100, 25000, 13.0)[:2] == (True, "CE")
    assert R.gap_decision(5, 0, 24900, 25000, 15.0)[:2] == (True, "PE")         # VIX <= 15 inclusive
    assert R.gap_decision(5, 0, 24900, 25000, 15.01)[0] is False
    assert R.gap_decision(4, 0, 25100, 25000, 12)[0] is False                    # short week
    assert R.gap_decision(6, 1, 25100, 25000, 12)[0] is True                     # 6-session cycle: day n-5
    assert R.gap_lots(1_000_000, 100) == 4                                       # 30k / 6.5k
    assert R.gap_lots(100_000, 80) == 1                                          # one lot allowed up to 6%
    assert R.gap_lots(100_000, 100) == 0
    assert R.gap_target_hit(100, 200) and not R.gap_target_hit(100, 199.9)


def test_rv20_matches_pandas():
    closes = list(25000 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.01, 40))))
    s = pd.Series(closes)
    want = np.log(s).diff().rolling(20).std().iloc[-1] * math.sqrt(252) * 100
    assert R.rv20(closes) == pytest.approx(want, rel=1e-12)
    assert R.rv20(closes[:20]) is None


def test_monthly_entry_exit_dte():
    assert R.mon_entry_due(45, 46) and R.mon_entry_due(42, 49)       # first session at <= 45
    assert not R.mon_entry_due(44, 45)                               # not the first such session
    assert not R.mon_entry_due(40, 47)                               # < 41 DTE: research skips
    assert R.mon_exit_due(21) and not R.mon_exit_due(22)


def test_forward_matches_research_chain():
    from research_monthly_straddle import Chain
    px = {}
    for k in range(24500, 25550, 50):
        px[(k, "CE")] = max(25030 - k, 0) + 120 * math.exp(-abs(k - 25030) / 400)
        px[(k, "PE")] = max(k - 25030, 0) + 120 * math.exp(-abs(k - 25030) / 400)
    c = Chain(spot=25010.0, px=px)
    simple = {k: (px[(k, "CE")], px[(k, "PE")]) for k in range(24500, 25550, 50)}
    assert R.forward(simple, 25010.0) == pytest.approx(c.forward())
    K, F = R.mon_strike(simple, 25010.0)
    assert K == min((k for k in simple), key=lambda k: abs(k - c.forward()))


def test_weekly_cycle_and_monthly_expiries():
    cyc = cal.weekly_cycle("2026-11-04", ["2026-11-10", "2026-11-17"], ["2026-11-03"])
    assert cyc["i"] == 0 and cyc["next_exp"] == "2026-11-10" and cyc["n"] == len(cyc["window"])
    assert cal.monthly_expiries(["2026-10-27", "2026-11-03", "2026-11-24", "2026-11-17", "2026-12-29"]) == \
        ["2026-10-27", "2026-11-24", "2026-12-29"]
