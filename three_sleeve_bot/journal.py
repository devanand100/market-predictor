"""
MongoDB journal (Atlas free tier: keep writes few, documents small, queries indexed).

Collections (every document carries v = schema version, sv = strategy version, run = run id, d = trading date):
  state      one document {_id: "bot"}: token, pause/flatten flags, lease, expiry cache, open positions,
             today's snapshot, MTM path, error counters, equity counters
  decisions  every rule evaluation, taken or skipped, with inputs and the reason
  trades     one per sleeve trade; src = paper | replay | research; legs[] hold the simulated fills
  days       one per trading day, written at EOD (equity, drawdown, margin, MTM path, run summary)
  recon      one per reconciled day (paper vs replay of the research rules)
  errors     first occurrence of an error signature per day (repeats only bump a counter in state)
  runs       one per non-TICK invocation (TTL 120 days)
"""
import datetime as dt
import os

SCHEMA_V = 1
_client = None
_db = None


def db():
    global _client, _db
    if _db is None:
        from pymongo import MongoClient
        _client = MongoClient(os.environ["MONGODB_URI"], maxPoolSize=1, serverSelectionTimeoutMS=5000,
                              connectTimeoutMS=5000, retryWrites=False, appname="three-sleeve-bot")
        _db = _client[os.environ.get("MONGO_DB", "three_sleeve")]
    return _db


def set_db(database):
    """Tests inject a mongomock database."""
    global _db
    _db = database


def ensure_indexes():
    from pymongo import ASCENDING as A
    x = db()
    x.decisions.create_index([("d", A), ("sleeve", A)])
    x.trades.create_index([("src", A), ("key", A)], unique=True)
    x.trades.create_index([("src", A), ("d", A)])
    x.trades.create_index([("ckey", A)])
    x.days.create_index([("d", A)], unique=True)
    x.recon.create_index([("d", A)], unique=True)
    x.errors.create_index([("d", A)])
    x.runs.create_index([("ts", A)], expireAfterSeconds=120 * 86400)


def stamp(doc, ctx):
    doc.setdefault("v", SCHEMA_V)
    doc.setdefault("sv", ctx.get("sv"))
    doc.setdefault("run", ctx.get("run"))
    doc.setdefault("d", ctx.get("d"))
    return doc


# ---------------------------------------------------------------- state
STATE_ID = "bot"
DEFAULT_STATE = {"_id": STATE_ID, "paused": False, "flatten": False, "lease": None, "dhan_token": None,
                 "token_upd": None, "exp_seen": [], "open": [], "snap": {}, "mtm": [], "err": {},
                 "realised": 0.0, "running": 0.0, "rc_months": [], "peak": None, "l2": None,
                 "recon_pending": [], "last_eod": None}


def get_state(projection=None):
    s = db().state.find_one({"_id": STATE_ID}, projection)
    if s is None:
        db().state.insert_one(dict(DEFAULT_STATE))
        s = dict(DEFAULT_STATE)
    return s


def update_state(set_=None, unset=None, push=None, inc=None, extra=None):
    u = {}
    if set_:
        u["$set"] = set_
    if unset:
        u["$unset"] = {k: "" for k in unset}
    if push:
        u["$push"] = push
    if inc:
        u["$inc"] = inc
    if extra:
        u.update(extra)
    if u:
        db().state.update_one({"_id": STATE_ID}, u, upsert=True)


def acquire_lease(run_id, seconds=150):
    """One run at a time. Returns False if another run holds an unexpired lease."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)   # Mongo returns naive UTC
    r = db().state.find_one_and_update(
        {"_id": STATE_ID, "$or": [{"lease": None}, {"lease.until": {"$lt": now}}]},
        {"$set": {"lease": {"run": run_id, "until": now + dt.timedelta(seconds=seconds)}}})
    if r is None and db().state.count_documents({"_id": STATE_ID}) == 0:
        get_state()
        return acquire_lease(run_id, seconds)
    return r is not None


def release_lease(run_id):
    db().state.update_one({"_id": STATE_ID, "lease.run": run_id}, {"$set": {"lease": None}})


# ---------------------------------------------------------------- writers
def decision(ctx, sleeve, outcome, why, inputs):
    db().decisions.insert_one(stamp({"sleeve": sleeve, "outcome": outcome, "why": why, "in": inputs,
                                     "ts": dt.datetime.now(dt.timezone.utc)}, ctx))


def insert_trade(ctx, doc):
    db().trades.insert_one(stamp(doc, ctx))


def close_trade(tid, fields):
    db().trades.update_one({"_id": tid}, {"$set": fields})


def upsert_day(ctx, d, fields):
    db().days.update_one({"d": d}, {"$set": stamp(fields, ctx)}, upsert=True)


def upsert_recon(ctx, d, fields):
    db().recon.update_one({"d": d}, {"$set": stamp(fields, ctx)}, upsert=True)


def run_summary(ctx, action, status, ms, info=None):
    db().runs.insert_one(stamp({"action": action, "status": status, "ms": ms, "info": info or {},
                                "ts": dt.datetime.now(dt.timezone.utc)}, ctx))


def record_error(ctx, action, exc_type, msg, tb):
    """-> True if this is the first time today for this signature (caller notifies); else just counts."""
    sig = f"{action}|{exc_type}|{msg[:80]}".replace(".", "_").replace("$", "_")
    s = db().state.find_one({"_id": STATE_ID}, {"err": 1}) or {}
    errs = s.get("err") or {}
    key = f"{ctx.get('d')}|{sig}"
    first = key not in errs
    db().state.update_one({"_id": STATE_ID}, {"$inc": {f"err.{key}": 1}}, upsert=True)
    if first:
        db().errors.insert_one(stamp({"action": action, "type": exc_type, "msg": msg[:500], "tb": tb[-2000:],
                                      "ts": dt.datetime.now(dt.timezone.utc)}, ctx))
    return first


def open_trades(src="paper"):
    return list(db().trades.find({"src": src, "status": "open"}))


def trades_between(src, d0, d1):
    return list(db().trades.find({"src": src, "d": {"$gte": d0, "$lte": d1}}))
