"""
Parity: replay.py + rules.py on the engine's 1-min history must reproduce the research trades exactly.
Skipped when the local engine cache (data/processed/engine_cache.pkl) is not present.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not (REPO / "data" / "processed" / "engine_cache.pkl").exists(),
                                reason="engine cache not built")


class EngineSource:
    def __init__(self, days):
        self.days = days

    def spot(self, day, m):
        return float(self.days[day].spot[m])

    def px(self, day, strike, typ, m):
        return self.days[day].px(strike, typ, m)


@pytest.fixture(scope="module")
def data():
    import research_positional as rp
    from engine import load_days, load_daily
    days, daily = load_days(), load_daily()
    return days, daily, rp.cycles(daily, days)


def test_l2_matches_research_institutional(data):
    import replay
    import research_institutional as ri
    days, daily, cyc = data
    sample = cyc[::9]                                  # ~33 cycles across 2021-2026, incl. short weeks
    src = EngineSource(days)
    checked = 0
    for window, exp in sample:
        want = ri.run(days, window, "L2")
        got = replay.replay_l2_cycle(src, window, (10 * 65) // 3)
        if want is None:
            assert got is None, window[0]
            continue
        tranches, pnl = got
        assert pnl == pytest.approx(want["pnl"], abs=1.0), window[0]
        checked += 1
    assert checked > 25


def test_gap_matches_final_gap_buy(data):
    import final_gap_buy as gb
    import replay
    days, daily, _ = data
    T = gb.trades(1.0).iloc[::4]
    src = EngineSource(days)
    idx = list(daily.index.strftime("%Y-%m-%d"))
    import research_positional as rp
    windows = {w[-5]: w[-5:] for w, e in rp.cycles(daily, days) if len(w) >= 5}
    for r in T.itertuples():
        w5 = windows[r.entry]
        prev_close = float(daily["close"].iloc[idx.index(r.entry) - 1])
        g = replay.replay_gap_cycle(src, w5, prev_close, 0.0)
        assert g is not None and g["typ"] == r.typ, r.entry
        assert g["fill"] == pytest.approx(r.buy, abs=1e-6), r.entry
        assert g["sell"] == pytest.approx(r.sell, abs=1e-6), r.entry
        assert ("target" if g["reason"] == "target" else "expiry") == r.reason, r.entry
