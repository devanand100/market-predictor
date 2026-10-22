"""Unit tests of the Strategy 6 rules."""
import pytest

import rules as R


def test_entry_days_and_vix_gates():
    n = 5                                     # full week: i = 0..4, expiry on i = 4
    assert R.decide("GAP", 0, n, 12.0)[0] and not R.decide("GAP", 0, n, 13.5)[0]
    assert R.decide("GAP", 0, n, 13.0)[0]                       # VIX <= 13 inclusive
    assert not R.decide("GAP", 1, n, 12.0)[0]
    assert R.decide("S2", 2, n, 15.0)[0] and not R.decide("S2", 2, n, 14.99)[0]
    assert not R.decide("S2", 3, n, 20.0)[0]
    assert R.decide("E0", 4, n, 16.0)[0] and not R.decide("E0", 3, n, 16.0)[0]
    assert not R.decide("S2", 2, n, None)[0]                    # no VIX -> no trade, never a guess
    assert not R.decide("GAP", 0, 4, 12.0)[0]                   # 4-session week: GAP day does not exist
    assert R.decide("GAP", 0, 6, 12.0)[0] is False and R.decide("GAP", 1, 6, 12.0)[0]


def test_strikes_match_research_rounding():
    s = R.strangle(25112.0, 160.0, 140.0, 1.25)                 # S = 300 -> 375 -> round(7.5) = 8 -> 400
    assert s == {"K": 25100, "S": 300.0, "w": 400, "ce": 25500, "pe": 24700}
    assert R.strangle(25100.0, 200.0, 170.0, 1.25)["w"] == 450  # 462.5 -> round(9.25) = 9 -> 450, allowed
    assert R.strangle(25100.0, 200.0, 190.0, 1.25) is None      # 487.5 -> 500 > 450: research skips
    assert R.strangle(25100.0, 40.0, 30.0, 0.5)["w"] == 50      # minimum width 50


def test_sizing_caps():
    lots, cap = R.short_lots(300_000, 22400, R.W_S2, False, 0)
    assert cap == pytest.approx(0.09 * 22400 * 65) and lots == 1
    lots, cap = R.short_lots(300_000, 22400, R.W_E0, True, 0)   # 11% on expiry day: 1 lot when nothing is open
    assert lots == 1 and cap == pytest.approx(0.11 * 22400 * 65)
    s2_margin = 0.09 * 22400 * 65
    assert R.short_lots(300_000, 22400, R.W_E0, True, s2_margin)[0] == 0   # S2 open -> 90% cap leaves no room
    assert R.short_lots(300_000, 23500, R.W_S2, False, 0)[0] == 1           # 55% keeps S2 tradable above 23,077
    lots, _ = R.short_lots(1_000_000, 22400, R.W_E0, True, 650_000)   # margin cap 90% binds
    assert lots == int((900_000 - 650_000) // (0.11 * 22400 * 65))
    lots, cap = R.gap_lots(300_000, 150.0, 0)
    assert cap == 150 * 65 and lots == 1


def test_exits():
    assert R.stop_level(60.0, R.S2_STOP) == 120.0 and R.stop_level(100.0, R.E0_STOP) == 150.0
    assert R.stop_hit(60.0, 120.0, 1.0) and not R.stop_hit(60.0, 119.9, 1.0)
    assert R.gap_target(101.0) == 202.0
    assert R.gap_target_hit(202.0, 202.05) and not R.gap_target_hit(202.0, 202.04)


# ---------------------------------------------------------------- monthly straddle (Strategy 7 book)
def test_rv20_matches_pandas():
    import math
    import numpy as np
    import pandas as pd
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
    import math
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


def test_s7_parameters_match_research():
    import research_realtrade_s7 as s7
    import final_realtrade_s7 as f7
    assert f7.W_MON == R.S7_W_MON and s7.DHAN_MARGIN == R.S7_MARGIN_PCT
    assert R.VRP_MIN == 2.0 and (R.MON_ENTRY_DTE, R.MON_ENTRY_MIN_DTE, R.MON_EXIT_DTE) == (45, 41, 21)
    import inspect
    src = inspect.getsource(s7.s6)
    assert "w2=0.45, we=0.45, fg=0.05" in src and (R.S7_W_S2, R.S7_W_E0, R.S7_F_GAP) == (0.45, 0.45, 0.05)


def test_mon_lots_match_research_sizing():
    lots, cap = R.mon_lots(1_500_000, 22800, 0)
    assert cap == pytest.approx(0.116 * 22800 * 65) and lots == int(0.20 * 1_500_000 // cap) == 1
    assert R.mon_lots(1_500_000, 22800, 1_300_000)[0] == 0            # 90% cap


def test_health_helpers():
    assert R.profit_factor([100, -50]) == 2.0 and R.profit_factor([100]) == float("inf") and R.profit_factor([]) is None
    leg = {"side": -1, "ltp": 100.0, "bid": 99.0, "ask": 101.0}
    assert R.spread_ratio(leg) == pytest.approx(1.0 / R.slip(100.0))
    assert R.spread_ratio({"side": 1, "ltp": 100.0, "bid": 0, "ask": 101.0}) is None
    closes = [(f"d{i}", 100.0 * (1.01 if i % 2 else 0.99)) for i in range(25)]
    vs = R.vrp_series([(d, 20.0) for d, _ in closes], closes)
    assert len(vs) == 5 and vs[-1][1] == pytest.approx(20.0 - R.rv20([c for _, c in closes]))


def test_kill_switch_rule():
    assert R.kill_switch(False, -0.10, [])[:2] == (False, None)
    assert R.kill_switch(False, -0.15, [])[:2] == (True, "fire")
    assert R.kill_switch(True, -0.18, [-100] * 20)[:2] == (True, None)           # PF 0 -> stays paused
    assert R.kill_switch(True, -0.18, [100] * 10)[:2] == (True, None)            # < 20 trades -> stays paused
    assert R.kill_switch(True, -0.18, [200, -100] * 10)[:2] == (False, "resume")  # PF 2.0 over 20 trades


def test_kill_switch_book_threshold():
    assert R.KILL_DD_S7 == -0.16
    assert R.kill_switch(False, -0.155, [], R.KILL_DD_S7)[:2] == (False, None)
    assert R.kill_switch(False, -0.16, [], R.KILL_DD_S7)[:2] == (True, "fire")
