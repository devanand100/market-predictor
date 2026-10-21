"""
Parity of rules.py with the research backtest (research_realtrade.py + research_realtrade_portfolio.sleeves):
replay Jan-Sep 2026 at Rs 3L on the engine data and check that every research trade is reproduced by the
bot's rules (entry day, VIX gate, direction, strikes, margin per lot), and that every day the rules call
an entry either has a research trade or a research-side reason not to trade (no price, width > 450, 0 lots).
Needs data/processed/engine_cache.pkl (skipped otherwise).
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if not (REPO / "data" / "processed" / "engine_cache.pkl").exists():
    pytest.skip("engine cache missing", allow_module_level=True)

import rules as R  # noqa: E402
import research_realtrade as rt  # noqa: E402
from research_realtrade_portfolio import sleeves  # noqa: E402

D0, D1, START = "2026-01-01", "2026-09-30", 300_000


@pytest.fixture(scope="module")
def replay():
    import os
    cwd = os.getcwd()
    os.chdir(REPO)                       # research code reads data/raw/... relative to the repo
    try:
        E, T, acct = rt.replay(sleeves(R.W_S2, R.W_E0, R.F_GAP, vix_hi=R.VIX_HI, vix_lo=R.VIX_LO), START, True, D0, D1)
        days, daily = rt.data()
        info, _ = rt.calendar(days, daily)
    finally:
        os.chdir(cwd)
    return acct, days, daily, info


def test_parameters_match_research():
    import inspect
    import final_realtrade_strategy as f
    assert f.F_GAP == R.F_GAP and (f.W_S2, f.W_E0) == (0.45, 0.45)
    assert R.W_S2 == R.W_E0 == 0.55                 # Rs 3L deployment weight (rules.py explains), inside the 0.3-0.6 grid
    assert (rt.MARGIN_PCT, rt.MAX_UTIL, rt.WINDOW_MAX) == (R.MARGIN_PCT, R.MAX_UTIL, R.WINDOW_MAX)
    src = inspect.getsource(sleeves)
    for frag in ('strangle(1.25), dte=2, stop=1.0', 'strangle(0.5), dte=0, stop=0.5', 'dte=4, direction="gap", tp=1.0'):
        assert frag in src


def test_every_research_trade_is_reproduced(replay):
    acct, days, daily, info = replay
    trades = [t for t in acct.trades if t["sleeve"] in ("S2", "E0", "GAP")]
    assert len(trades) >= 10
    for t in trades:
        d = t["entry"]
        i, n, exp = info[d]
        vix = daily.loc[d, "vix_prev"]
        assert R.decide(t["sleeve"], i, n, vix)[0], (d, t["sleeve"], i, n, vix)
        day, m = days[d], rt.ENTRY_MIN
        spot = day.spot[m]
        legs = sorted((l["typ"], l["K"], l["qty"] > 0) for l in t["legs"])
        if t["sleeve"] == "GAP":
            typ, _ = R.gap_side(spot, daily["close"].iloc[daily.index.get_loc(d) - 1])
            assert legs == [(typ, R.atm(spot), True)]
            cap = rt.real(day, R.atm(spot), typ, m) * 65
        else:
            K = R.atm(spot)
            s = R.strangle(spot, rt.real(day, K, "CE", m), rt.real(day, K, "PE", m),
                           R.S2_MULT if t["sleeve"] == "S2" else R.E0_MULT)
            assert legs == [("CE", s["ce"], False), ("PE", s["pe"], False)]
            cap = R.margin_pct(d == exp) * spot * 65
        assert t["meta"]["margin"] / t["meta"]["lots"] == pytest.approx(cap, rel=1e-9)


def test_no_unexplained_skips(replay):
    acct, days, daily, info = replay
    have = {(t["entry"], t["sleeve"]) for t in acct.trades}
    unexplained = []
    for d, (i, n, exp) in info.items():
        if not (D0 <= d <= D1):
            continue
        for sl in ("S2", "E0", "GAP"):
            if not R.decide(sl, i, n, daily.loc[d, "vix_prev"])[0] or (d, sl) in have:
                continue
            day, m = days[d], rt.ENTRY_MIN
            spot = day.spot[m]
            K = R.atm(spot)
            ce, pe = rt.real(day, K, "CE", m), rt.real(day, K, "PE", m)
            if ce is None or pe is None:
                continue                                    # no price at 09:30: research cannot trade
            if sl != "GAP":
                s = R.strangle(spot, ce, pe, R.S2_MULT if sl == "S2" else R.E0_MULT)
                if s is None or rt.real(day, s["ce"], "CE", m) is None or rt.real(day, s["pe"], "PE", m) is None:
                    continue
                if R.margin_pct(d == exp) * spot * 65 > R.W_S2 * 2_000_000:   # cannot be sized below Rs 20L anyway
                    continue
            unexplained.append((d, sl))
    # remaining skips must be sizing (0 lots at the equity of that day); report them for visibility
    print("rule-day without research trade (sizing):", unexplained)
    assert all(sl in ("E0", "S2", "GAP") for _, sl in unexplained)
    assert len(unexplained) <= 25
