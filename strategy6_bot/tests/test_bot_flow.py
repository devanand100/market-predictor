"""
End-to-end flow through lambda_handler with a fake Dhan and an in-memory Mongo (mongomock):
a high-VIX week (S2 entry 2 sessions before expiry, S2 stop, E0 entry on expiry day, 15:15 exit),
a low-VIX week (gap buy, limit target), sizing at Rs 3L, pause / flatten, holiday skip, ONE Discord
message per run, error de-duplication and the Mongo write budget with 1-minute ticks.
"""
import math

import mongomock
import pytest

import journal as J
import lambda_function as L
import market_calendar as cal
import notify as N
import rules as R

PREV_EXP, EXP = "2026-12-01", "2026-12-08"
WEEK = ["2026-12-02", "2026-12-03", "2026-12-04", "2026-12-07", "2026-12-08"]   # i = 0..4, expiry Tuesday


class FakeDhan:
    """Option prices from a simple smile around `spot`; tests move spot / override single prices."""
    spot = 22400.0
    vix_prev = 16.0
    prev_close = 22300.0
    overrides = {}
    fail_ltp = False
    expiries = [EXP, "2026-12-15", "2026-12-22", "2026-12-29"]

    def __init__(self, client_id, token, min_gap_s=0.0):
        pass

    @staticmethod
    def sid(exp, K, typ):
        return str(int(exp.replace("-", "")[2:]) * 100000 + K // 50 * 10 + (1 if typ == "CE" else 2))

    @classmethod
    def price(cls, exp, K, typ):
        k = cls.sid(exp, K, typ)
        if k in cls.overrides:
            return cls.overrides[k]
        intrinsic = max(cls.spot - K, 0) if typ == "CE" else max(K - cls.spot, 0)
        return round(intrinsic + 150 * math.exp(-abs(K - cls.spot) / 250), 2)

    def token_info(self, tok=None):
        return True, "31/12/2026 23:59"

    def renew(self):
        return "x" * 60 + "." + "y" * 60 + "." + "z" * 10

    def expiry_list(self):
        return list(self.expiries)

    def option_chain(self, expiry):
        chain = {}
        base = int(self.spot // 50 * 50)
        for K in range(base - 1500, base + 1550, 50):
            chain[K] = {t.lower(): {"sid": self.sid(expiry, K, t), "ltp": self.price(expiry, K, t),
                                    "bid": self.price(expiry, K, t) - 0.5, "ask": self.price(expiry, K, t) + 0.5,
                                    "iv": 14.0, "oi": 1000} for t in ("CE", "PE")}
        return self.spot, chain

    def ltp(self, fno_ids=(), with_index=True):
        if FakeDhan.fail_ltp:
            raise RuntimeError("Dhan 503 on /v2/marketfeed/ltp: upstream unavailable")
        out = {}
        for s in fno_ids:
            s = str(s)
            exp = f"20{s[:2]}-{s[2:4]}-{s[4:6]}"
            K = int(s[6:]) // 10 * 50
            out[s] = self.price(exp, K, "CE" if s.endswith("1") else "PE")
        out["spot"], out["vix"] = self.spot, self.vix_prev
        return out

    def daily_closes(self, security_id, before, days=45):
        prev = cal.prev_trading_day(before)
        if security_id == L.VIX_ID:
            return [(prev, self.vix_prev)]
        return [(prev, self.prev_close)]

    def margin(self, legs):
        return {"total": 123456.0, "span": 100000.0, "exposure": 23456.0, "hedge_benefit": 0.0}


@pytest.fixture()
def bot(monkeypatch):
    db = mongomock.MongoClient()["strategy6_test"]
    J.set_db(db)
    monkeypatch.setattr(L, "Dhan", FakeDhan)
    monkeypatch.setattr(L, "CAPITAL", 300_000.0)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    FakeDhan.spot, FakeDhan.vix_prev, FakeDhan.prev_close, FakeDhan.overrides, FakeDhan.fail_ltp = 22400.0, 16.0, 22300.0, {}, False
    N.sent.clear()
    L.lambda_handler({"action": "INIT"})
    J.update_state(set_={"exp_seen": [PREV_EXP]})
    return db


def run(action, date, time=None, **kw):
    before = len(N.sent)
    out = L.lambda_handler({"action": action, "date": date, **({"time": time} if time else {}), **kw})
    assert len(N.sent) - before <= 1, f"{action} produced more than one Discord message"
    return out


def outcomes(out):
    return {d["sleeve"]: d["outcome"] for d in out["decisions"]}


def test_week_is_a_full_cycle():
    cyc = cal.weekly_cycle(WEEK[0], [EXP], [PREV_EXP])
    assert cyc["window"] == WEEK


def test_high_vix_week(bot):
    db = bot
    hb = run("HEARTBEAT", WEEK[2])
    assert any(p.startswith("S2 entry") for p in hb["plan"])
    assert outcomes(run("ENTRY_0931", WEEK[0])) == {"GAP": "skip", "S2": "skip", "E0": "skip"}   # VIX 16 > 13
    assert outcomes(run("ENTRY_0931", WEEK[1])) == {"GAP": "skip", "S2": "skip", "E0": "skip"}

    out = run("ENTRY_0931", WEEK[2])                     # 2 sessions to expiry
    assert outcomes(out)["S2"] == "enter"
    s2 = db.trades.find_one({"sleeve": "S2"})
    S = 2 * 150.0                                        # ATM CE + PE in the fake smile
    w = R.rnd(R.S2_MULT * S)
    assert [l["K"] for l in s2["legs"]] == [22400 + w, 22400 - w] and s2["lots"] == 1 and s2["qty"] == 65
    assert s2["stop_sum"] == pytest.approx(2 * s2["px_in"], abs=0.01)
    assert s2["margin_model"] == round(0.09 * 22400 * 65)

    assert run("TICK", WEEK[2], "11:00")["closed"] == []
    FakeDhan.spot = 23000.0                              # rally through the call
    r = run("TICK", WEEK[2], "13:07")
    assert s2["_id"] in r["closed"]
    t = db.trades.find_one({"_id": s2["_id"]})
    assert t["exit_reason"].startswith("stop") and t["net"] < 0
    assert set(t["charges"]) == {"brokerage", "stt", "exch", "sebi", "stamp", "gst"}
    run("EOD", WEEK[2])

    FakeDhan.spot = 22400.0
    out = run("ENTRY_0931", WEEK[4])                     # after the S2 loss equity < Rs 2.91L: no E0 lot
    e0 = next(d for d in out["decisions"] if d["sleeve"] == "E0")
    assert e0["outcome"] == "skip" and "equity too small" in e0["why"]
    day = db.days.find_one({"d": WEEK[2]})
    assert day["equity"] == pytest.approx(300_000 + J.get_state()["realised"] - R.MONTHLY_RUNNING_COSTS, abs=1)


def test_expiry_day_strangle(bot):
    db = bot
    out = run("ENTRY_0931", WEEK[4])                     # expiry day, nothing open: E0 gets one lot at Rs 3L
    assert outcomes(out)["E0"] == "enter", out["decisions"]
    e0 = db.trades.find_one({"sleeve": "E0"})
    assert e0["legs"][0]["K"] - e0["legs"][1]["K"] == 2 * R.rnd(R.E0_MULT * 300.0)
    assert e0["margin_model"] == round(0.11 * 22400 * 65)
    assert e0["stop_sum"] == pytest.approx(1.5 * e0["px_in"], abs=0.01)
    assert run("TICK", WEEK[4], "15:15")["closed"] == []
    r = run("TICK", WEEK[4], "15:16")
    assert r["closed"] == [e0["_id"]] and r["open"] == 0
    assert db.trades.find_one({"_id": e0["_id"]})["exit_reason"].startswith("time")
    run("EOD", WEEK[4])


def test_e0_blocked_by_open_s2_at_3L(bot):
    run("ENTRY_0931", WEEK[2])
    out = run("ENTRY_0931", WEEK[4])
    e0 = next(d for d in out["decisions"] if d["sleeve"] == "E0")
    assert e0["outcome"] == "skip" and "equity too small" in e0["why"]


def test_low_vix_gap_week(bot):
    db = bot
    FakeDhan.vix_prev, FakeDhan.prev_close = 12.5, 22300.0
    out = run("ENTRY_0931", WEEK[0])                     # 4 sessions to expiry, gap +0.45%
    assert outcomes(out) == {"GAP": "enter", "S2": "skip", "E0": "skip"}
    g = db.trades.find_one({"sleeve": "GAP"})
    assert g["legs"][0]["typ"] == "CE" and g["legs"][0]["K"] == 22400 and g["lots"] == 1
    sid = g["legs"][0]["sid"]
    FakeDhan.overrides[sid] = g["target"] + 0.04         # not through the limit yet
    assert run("TICK", WEEK[0], "10:00")["closed"] == []
    FakeDhan.overrides[sid] = g["target"] + 30           # trades through: filled AT the limit, not above
    r = run("TICK", WEEK[0], "10:01")
    assert g["_id"] in r["closed"]
    t = db.trades.find_one({"_id": g["_id"]})
    assert t["exit_legs"][0]["fill"] == pytest.approx(g["target"]) and t["net"] > 0
    assert outcomes(run("ENTRY_0931", WEEK[2]))["S2"] == "skip"     # VIX 12.5 < 15


def test_gap_down_buys_put_and_exits_at_expiry(bot):
    db = bot
    FakeDhan.vix_prev, FakeDhan.prev_close = 12.0, 22500.0
    run("ENTRY_0931", WEEK[0])
    g = db.trades.find_one({"sleeve": "GAP"})
    assert g["legs"][0]["typ"] == "PE"
    for d in WEEK[1:4]:
        run("TICK", d, "12:00")
    r = run("TICK", WEEK[4], "15:16")
    assert r["closed"] == [g["_id"]]
    assert db.trades.find_one({"_id": g["_id"]})["exit_reason"].startswith("time")


def test_pause_and_flatten(bot):
    db = bot
    run("PAUSE", WEEK[2])
    assert all(d["outcome"] == "skip" for d in run("ENTRY_0931", WEEK[2])["decisions"])
    run("RESUME", WEEK[2])
    assert outcomes(run("ENTRY_0931", WEEK[2]))["S2"] == "enter"
    run("FLATTEN", WEEK[2])
    r = run("TICK", WEEK[2], "11:01")
    assert r["open"] == 0 and len(r["closed"]) == 1
    assert all(t["exit_reason"] == "flatten" for t in db.trades.find({"status": "closed"}))
    assert J.get_state()["flatten"] is False


def test_test_action_records_nothing(bot):
    out = run("TEST", WEEK[2])
    assert outcomes(out)["S2"] == "enter" and out["dry_run"]
    assert bot.trades.count_documents({}) == 0 and not J.get_state().get("open")


def test_holiday_skip_touches_nothing(bot, monkeypatch):
    monkeypatch.setattr(J, "_db", None)              # any Mongo access would now try a real connection
    assert "skipped" in L.lambda_handler({"action": "TICK", "date": "2026-12-05"})          # Saturday
    assert "skipped" in L.lambda_handler({"action": "ENTRY_0931", "date": "2026-11-10"})    # NSE holiday


def test_errors_are_notified_once_per_day(bot):
    run("ENTRY_0931", WEEK[2])
    FakeDhan.fail_ltp = True
    before = len(N.sent)
    for _ in range(50):
        with pytest.raises(RuntimeError):
            L.lambda_handler({"action": "TICK", "date": WEEK[2], "time": "11:01"})
    assert len(N.sent) - before == 1
    assert sum(J.get_state()["err"].values()) == 50


def test_write_budget_one_day_with_minute_ticks(bot):
    counts = {"n": 0}
    for name in ("insert_one", "update_one", "find_one_and_update", "insert_many", "update_many"):
        orig = getattr(mongomock.collection.Collection, name)

        def wrapped(self, *a, _orig=orig, **k):
            counts["n"] += 1
            return _orig(self, *a, **k)
        setattr(mongomock.collection.Collection, name, wrapped)
    try:
        run("HEARTBEAT", WEEK[2])
        run("ENTRY_0931", WEEK[2])
        ticks = [f"09:{m}" for m in range(32, 60)] + [f"{h}:{m:02d}" for h in range(10, 15) for m in range(60)] \
            + [f"15:{m:02d}" for m in range(17)]          # the deployed TICK schedule
        for t in ticks:
            run("TICK", WEEK[2], t)
        run("EOD", WEEK[2])
    finally:
        import importlib
        importlib.reload(mongomock.collection)
    assert len(ticks) == 345
    assert counts["n"] < 40, counts
