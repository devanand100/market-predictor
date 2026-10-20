"""
End-to-end flow through lambda_handler with a fake Dhan and an in-memory Mongo (mongomock):
one full weekly cycle (L2 tranches, gap buy, gap target, L2 stop, expiry exit, EOD), monthly entry/exit,
pause / flatten, holiday skip, ONE Discord message per run, error de-duplication and the Mongo write budget.
"""
import math

import mongomock
import pytest

import journal as J
import lambda_function as L
import market_calendar as cal
import notify as N

PREV_EXP, EXP = "2026-12-01", "2026-12-08"
WEEK = ["2026-12-02", "2026-12-03", "2026-12-04", "2026-12-07", "2026-12-08"]
MON_EXP = "2026-12-29"


class FakeDhan:
    """Option prices from a simple smile around `spot`; tests move spot / override single prices."""
    spot = 25100.0
    vix = 13.0
    prev_close = 25000.0
    overrides = {}
    fail_ltp = False
    expiries = [EXP, "2026-12-15", "2026-12-22", MON_EXP]

    def __init__(self, client_id, token, min_gap_s=0.0):
        self.calls = 0

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
        for K in range(int(self.spot // 50 * 50) - 1500, int(self.spot // 50 * 50) + 1550, 50):
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
        out["spot"], out["vix"] = self.spot, self.vix
        return out

    def daily_closes(self, security_id, before, days=45):
        if security_id == 21:
            return [(cal.prev_trading_day(before), 16.5)]
        return [(f"d{i}", 25000 * (1 + 0.001 * math.sin(i))) for i in range(30)][:-1] + [(cal.prev_trading_day(before), self.prev_close)]

    def margin(self, legs):
        return {"total": 123456.0, "span": 100000.0, "exposure": 23456.0, "hedge_benefit": 0.0}


@pytest.fixture()
def bot(monkeypatch):
    db = mongomock.MongoClient()["three_sleeve_test"]
    J.set_db(db)
    monkeypatch.setattr(L, "Dhan", FakeDhan)
    monkeypatch.setattr(L, "CAPITAL", 1_500_000.0)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    FakeDhan.spot, FakeDhan.vix, FakeDhan.prev_close, FakeDhan.overrides, FakeDhan.fail_ltp = 25100.0, 13.0, 25000.0, {}, False
    N.sent.clear()
    L.lambda_handler({"action": "INIT"})
    J.update_state(set_={"exp_seen": [PREV_EXP]})
    return db


def run(action, date, time=None, **kw):
    before = len(N.sent)
    out = L.lambda_handler({"action": action, "date": date, **({"time": time} if time else {}), **kw})
    assert len(N.sent) - before <= 1, f"{action} produced more than one Discord message"
    return out


def test_week_is_a_full_cycle():
    cyc = cal.weekly_cycle(WEEK[0], [EXP], [PREV_EXP])
    assert cyc["window"] == WEEK


def test_full_week(bot):
    db = bot
    run("HEARTBEAT", WEEK[0])
    run("SNAPSHOT_0920", WEEK[0])
    out = run("ENTRY_0931", WEEK[0])
    outcomes = {d["sleeve"]: d["outcome"] for d in out["decisions"]}
    assert outcomes == {"L2": "enter", "GAP": "enter"}
    st = J.get_state()
    assert {x["sl"] for x in st["open"]} == {"L2", "GAP"}
    l2 = db.trades.find_one({"sleeve": "L2"})
    assert l2["lots"] == 1 and l2["legs"][0]["K"] - l2["legs"][1]["K"] == 2 * l2["in"]["w"]
    gap = db.trades.find_one({"sleeve": "GAP"})
    assert gap["legs"][0]["typ"] == "CE" and gap["legs"][0]["K"] == 25100

    # gap target: push the CE above 2x the fill
    FakeDhan.overrides[gap["legs"][0]["sid"]] = gap["target"] + 1
    r = run("TICK", WEEK[0], "10:01")
    assert gap["_id"] in r["closed"]
    g = db.trades.find_one({"_id": gap["_id"]})
    assert g["status"] == "closed" and g["exit_reason"].startswith("target") and g["net"] > 0
    assert set(g["charges"]) == {"brokerage", "stt", "exch", "sebi", "stamp", "gst"}
    run("EOD", WEEK[0])

    # day 2: second tranche; then a rally stops tranche 1 (and maybe 2)
    FakeDhan.overrides = {}
    FakeDhan.spot = 25150.0
    out = run("ENTRY_0931", WEEK[1])
    assert {d["sleeve"]: d["outcome"] for d in out["decisions"]}["L2"] == "enter"
    FakeDhan.spot = 25900.0
    r = run("TICK", WEEK[1], "13:01")
    assert l2["_id"] in r["closed"]
    assert db.trades.find_one({"_id": l2["_id"]})["exit_reason"].startswith("stop")
    run("EOD", WEEK[1])

    # day 3: third tranche; days 4-5 no tranche; expiry exit at 15:16
    FakeDhan.spot = 25200.0
    run("ENTRY_0931", WEEK[2])
    out = run("ENTRY_0931", WEEK[3])
    assert {d["sleeve"]: d["outcome"] for d in out["decisions"]} == {"L2": "skip", "GAP": "skip"}
    r = run("TICK", WEEK[4], "15:11")
    still = r["open"]
    r = run("TICK", WEEK[4], "15:16")
    assert r["open"] == still - len(r["closed"]) and r["open"] == 0
    run("EOD", WEEK[4])
    day = db.days.find_one({"d": WEEK[4]})
    assert day["equity"] == pytest.approx(1_500_000 + J.get_state()["realised"] - 1990, abs=1)
    assert db.trades.count_documents({"src": "paper", "status": "closed"}) == 4


def test_monthly_entry_and_exit(bot):
    db = bot
    day = next(d for d in ["2026-11-13", "2026-11-16", "2026-11-17"]
               if 41 <= cal.dte(MON_EXP, d) <= 45 < cal.dte(MON_EXP, cal.prev_trading_day(d)))
    out = run("MONTHLY_1015", day)
    dec = out["decisions"][0]
    assert dec["outcome"] == "enter", dec
    t = db.trades.find_one({"sleeve": "MON"})
    assert t["legs"][0]["K"] == t["legs"][1]["K"] and t["lots"] == 2
    exit_day = next(d for d in cal.sessions_between(day, MON_EXP) if cal.dte(MON_EXP, d) <= 21)
    out = run("MONTHLY_1015", exit_day)
    assert out["exits"] == [t["_id"]]
    assert db.trades.find_one({"_id": t["_id"]})["status"] == "closed"


def test_pause_and_flatten(bot):
    db = bot
    run("PAUSE", WEEK[0])
    out = run("ENTRY_0931", WEEK[0])
    assert all(d["outcome"] == "skip" for d in out["decisions"])
    run("RESUME", WEEK[0])
    J.update_state(set_={"l2": None})
    out = run("ENTRY_0931", WEEK[0])
    assert any(d["outcome"] == "enter" for d in out["decisions"])
    run("FLATTEN", WEEK[0])
    r = run("TICK", WEEK[0], "11:01")
    assert r["open"] == 0 and len(r["closed"]) >= 1
    assert all(t["exit_reason"] == "flatten" for t in db.trades.find({"status": "closed"}))
    assert J.get_state()["flatten"] is False


def test_holiday_skip_touches_nothing(bot, monkeypatch):
    monkeypatch.setattr(J, "_db", None)              # any Mongo access would now try a real connection
    assert "skipped" in L.lambda_handler({"action": "TICK", "date": "2026-12-05"})   # Saturday
    assert "skipped" in L.lambda_handler({"action": "ENTRY_0931", "date": "2026-11-10"})   # NSE holiday


def test_errors_are_notified_once_per_day(bot):
    run("ENTRY_0931", WEEK[0])
    FakeDhan.fail_ltp = True
    before = len(N.sent)
    for _ in range(50):
        with pytest.raises(RuntimeError):
            L.lambda_handler({"action": "TICK", "date": WEEK[0], "time": "11:01"})
    assert len(N.sent) - before == 1
    errs = J.get_state()["err"]
    assert sum(errs.values()) == 50


def test_write_budget_one_day(bot):
    db = bot
    counts = {"n": 0}
    for name in ("insert_one", "update_one", "find_one_and_update", "insert_many", "update_many"):
        orig = getattr(mongomock.collection.Collection, name)

        def wrapped(self, *a, _orig=orig, **k):
            counts["n"] += 1
            return _orig(self, *a, **k)
        setattr(mongomock.collection.Collection, name, wrapped)
    try:
        run("HEARTBEAT", WEEK[0])
        run("SNAPSHOT_0920", WEEK[0])
        run("ENTRY_0931", WEEK[0])
        run("MONTHLY_1015", WEEK[0])
        ticks = [f"09:{m}" for m in (36, 41, 46, 51, 56)] + [f"{h}:{m:02d}" for h in range(10, 15) for m in range(1, 60, 5)] \
            + [f"15:{m:02d}" for m in (1, 6, 11, 16, 21, 26, 31)]
        for t in ticks:                               # the deployed TICK schedule
            run("TICK", WEEK[0], t)
        run("EOD", WEEK[0])
    finally:
        import importlib
        importlib.reload(mongomock.collection)
    assert counts["n"] < 40, counts
