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
    monkeypatch.setattr(J, "_client", None)          # any Mongo access would now try a real connection
    monkeypatch.delenv("MONGODB_URI", raising=False)
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


# ---------------------------------------------------------------- Dhan margin check before entry
def _margin_per_lot(rs):
    """Fake Dhan margin: rs per lot of the strangle (both legs), None = API unavailable."""
    def m(self, legs):
        if rs is None:
            return None
        lots = max(l["qty"] for l in legs) / 65
        return {"total": rs * lots, "span": 0.0, "exposure": 0.0, "hedge_benefit": 0.0}
    return m


def test_dhan_margin_recorded_and_shown(bot, monkeypatch):
    monkeypatch.setattr(FakeDhan, "margin", _margin_per_lot(171_605.0))
    out = run("ENTRY_0931", WEEK[2])
    d = next(x for x in out["decisions"] if x["sleeve"] == "S2")
    assert d["outcome"] == "enter" and d["in"]["margin_dhan"] == 171_605 and d["in"]["margin_check"] == "dhan"
    t = bot.trades.find_one({"sleeve": "S2"})
    assert t["margin_dhan"] == 171_605 and t["margin_available"] == 270_000
    assert J.get_state()["open"][0]["mgd"] == 171_605                  # later entries count the real margin
    embeds = N.sent[-1]["embeds"]
    entry = next(e for e in embeds if e["title"].endswith("Entry S2"))
    assert any(f["name"] == "Margin model / Dhan" and "171,605" in f["value"] for f in entry["fields"])


def test_dhan_margin_too_high_skips(bot, monkeypatch):
    monkeypatch.setattr(FakeDhan, "margin", _margin_per_lot(280_000.0))        # > 90% of Rs 3L
    out = run("ENTRY_0931", WEEK[2])
    d = next(x for x in out["decisions"] if x["sleeve"] == "S2")
    assert d["outcome"] == "skip" and "Dhan margin" in d["why"] and bot.trades.count_documents({}) == 0


def test_dhan_margin_cuts_lots(bot, monkeypatch):
    monkeypatch.setattr(L, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(FakeDhan, "margin", _margin_per_lot(250_000.0))
    out = run("ENTRY_0931", WEEK[2])
    d = next(x for x in out["decisions"] if x["sleeve"] == "S2")
    model_lots = R.short_lots(1_000_000, 22400, R.W_S2, False, 0)[0]
    assert model_lots == 4 and d["outcome"] == "enter" and d["in"]["lots"] == 3          # 4 x 250k > 900k -> 3
    assert "cut 4 → 3" in d["why"] and bot.trades.find_one({"sleeve": "S2"})["margin_dhan"] == 750_000


def test_dhan_margin_unavailable_enters_on_model_and_says_so(bot, monkeypatch):
    monkeypatch.setattr(FakeDhan, "margin", _margin_per_lot(None))
    out = run("ENTRY_0931", WEEK[2])
    d = next(x for x in out["decisions"] if x["sleeve"] == "S2")
    assert d["outcome"] == "enter" and d["in"]["margin_check"].startswith("unavailable")
    entry = next(e for e in N.sent[-1]["embeds"] if e["title"].endswith("Entry S2"))
    assert any("Dhan check unavailable" in f["value"] for f in entry["fields"])


def test_open_dhan_margin_blocks_expiry_day_lot(bot, monkeypatch):
    monkeypatch.setattr(FakeDhan, "margin", _margin_per_lot(171_605.0))
    run("ENTRY_0931", WEEK[2])                                  # S2 open with Rs 1.72L real margin
    out = run("ENTRY_0931", WEEK[4])
    e0 = next(d for d in out["decisions"] if d["sleeve"] == "E0")
    assert e0["outcome"] == "skip"


# ---------------------------------------------------------------- two books in one Lambda (Strategy 6 + Strategy 7)
MON_EXP = "2026-12-29"


@pytest.fixture()
def two_books(bot, monkeypatch):
    monkeypatch.setattr(L, "CAPITAL_S7", 1_500_000.0)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL_S7", raising=False)
    monkeypatch.delenv("MONGO_DB_S7", raising=False)
    s7 = bot.client[bot.name + "_s7"]
    J.use(s7.name)
    J.update_state(set_={"exp_seen": [PREV_EXP]})
    J.use(None)
    calls = {"oc": 0}
    orig = FakeDhan.option_chain

    def counted(self, expiry):
        calls["oc"] += 1
        return orig(self, expiry)
    monkeypatch.setattr(FakeDhan, "option_chain", counted)

    def closes(self, security_id, before, days=45):
        prev = cal.prev_trading_day(before)
        if security_id == L.VIX_ID:
            return [(prev, self.vix_prev)]
        hist = [22000 * (1 + 0.002 * math.sin(i)) for i in range(40)]
        return [(f"d{i}", c) for i, c in enumerate(hist[:-1])] + [(prev, self.prev_close)]
    monkeypatch.setattr(FakeDhan, "daily_closes", closes)
    return bot, s7, calls


def run_books(action, date, time=None, **kw):
    before = len(N.sent)
    out = L.lambda_handler({"action": action, "date": date, **({"time": time} if time else {}), **kw})
    books = [m["book"] for m in N.sent[before:]]
    assert len(books) == len(set(books)), f"{action}: more than one Discord message for a book {books}"
    return out


def test_two_books_trade_separately_with_one_chain_call(two_books):
    s6db, s7db, calls = two_books
    out = run_books("ENTRY_0931", WEEK[2])
    assert set(out) == {"s6", "s7"}
    assert calls["oc"] == 1                                     # one option chain shared by both books
    t6, t7 = s6db.trades.find_one({"sleeve": "S2"}), s7db.trades.find_one({"sleeve": "S2"})
    assert t6["lots"] == 1 and t6["margin_model"] == round(0.09 * 22400 * 65)         # Rs 3L, 9% model, 0.55
    assert t7["lots"] == R.short_lots(1_500_000, 22400, 0.45, False, 0, 0.116)[0] == 3  # Rs 15L, 11.6% model, 0.45
    assert t7["margin_model"] == round(0.116 * 22400 * 65 * 3)
    msgs = {m["book"]: m for m in N.sent[-2:]}
    assert set(msgs) == {"s6", "s7"}
    assert msgs["s7"]["embeds"][-1]["footer"]["text"].startswith("Strategy 7")
    assert not s7db.state.find_one({"_id": J.STATE_ID}).get("dhan_token")   # token lives in the primary book only


def test_tick_prefetches_once_and_closes_each_book(two_books, monkeypatch):
    s6db, s7db, _ = two_books
    run_books("ENTRY_0931", WEEK[2])
    ltp_calls = {"n": 0}
    orig = FakeDhan.ltp

    def counted(self, ids=(), with_index=True):
        ltp_calls["n"] += 1
        return orig(self, ids, with_index)
    monkeypatch.setattr(FakeDhan, "ltp", counted)
    FakeDhan.spot = 23000.0
    out = run_books("TICK", WEEK[2], "13:07")
    assert ltp_calls["n"] == 1
    assert out["s6"]["closed"] and out["s7"]["closed"]
    assert s6db.trades.find_one({"sleeve": "S2"})["exit_reason"].startswith("stop")
    assert s7db.trades.find_one({"sleeve": "S2"})["exit_reason"].startswith("stop")


def test_monthly_straddle_only_in_strategy7(two_books):
    s6db, s7db, _ = two_books
    day = next(d for d in ["2026-11-13", "2026-11-16", "2026-11-17"]
               if 41 <= cal.dte(MON_EXP, d) <= 45 < cal.dte(MON_EXP, cal.prev_trading_day(d)))
    out = run_books("MONTHLY_1015", day)
    assert set(out) == {"s7"}
    dec = out["s7"]["decisions"][0]
    assert dec["outcome"] == "enter", dec
    t = s7db.trades.find_one({"sleeve": "MON"})
    assert t["legs"][0]["K"] == t["legs"][1]["K"] and t["lots"] >= 1 and t["margin_dhan"] == 123456
    assert s6db.trades.count_documents({"sleeve": "MON"}) == 0
    exit_day = next(d for d in cal.sessions_between(day, MON_EXP) if cal.dte(MON_EXP, d) <= 21)
    run_books("TICK", exit_day, "11:00")                         # no stop and no 15:15 exit for the monthly
    assert s7db.trades.find_one({"_id": t["_id"]})["status"] == "open"
    out = run_books("MONTHLY_1015", exit_day)
    assert out["s7"]["exits"] == [t["_id"]]
    assert s7db.trades.find_one({"_id": t["_id"]})["status"] == "closed"


def test_one_book_failing_does_not_stop_the_other(two_books, monkeypatch):
    s6db, s7db, _ = two_books
    orig = R.short_lots

    def broken(equity, spot, weight, expiry_day, used, base=None):
        if base == R.S7_MARGIN_PCT:
            raise ValueError("s7 sizing exploded")
        return orig(equity, spot, weight, expiry_day, used, base)
    monkeypatch.setattr(R, "short_lots", broken)
    with pytest.raises(RuntimeError, match="s7"):
        L.lambda_handler({"action": "ENTRY_0931", "date": WEEK[2]})
    assert s6db.trades.find_one({"sleeve": "S2"}) is not None and s7db.trades.count_documents({}) == 0
    msgs = {m["book"]: m for m in N.sent[-2:]}
    assert any("failed" in e["title"] for e in msgs["s7"]["embeds"])
    assert not any("failed" in e["title"] for e in msgs["s6"]["embeds"])


def test_book_filter_and_primary_only_actions(two_books):
    s6db, s7db, _ = two_books
    out = run_books("STATUS", WEEK[2], book="s7")
    assert out["capital"] == 1_500_000 and out["book"] == "Strategy 7"
    run_books("PAUSE", WEEK[2], book="s7")
    out = run_books("ENTRY_0931", WEEK[2])
    assert {d["outcome"] for d in out["s7"]["decisions"]} == {"skip"} and s6db.trades.count_documents({}) == 1
    out = run_books("RENEW_TOKEN", WEEK[2])
    assert out["renewed"] and s6db.state.find_one({"_id": J.STATE_ID})["dhan_token"].startswith("x")
