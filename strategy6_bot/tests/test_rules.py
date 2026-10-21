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
