"""
IST clock, NSE trading days, expiries and the weekly cycle.

A weekly cycle is every trading session after the previous weekly expiry up to and including the next
one (research_positional.cycles). Expiry dates always come from Dhan's expiry list; the list drops an
expiry once it has passed, so past expiries are cached in Mongo state (`exp_seen`).
"""
import datetime as dt
import os

# holidays reads its own package metadata, which can be missing inside a zipped Lambda
try:
    import importlib.metadata as _md
    _orig_version = _md.version

    def _safe_version(name):
        try:
            return _orig_version(name)
        except Exception:
            return "0.0"
    _md.version = _safe_version
except Exception:
    pass
import holidays  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ist_now():
    return dt.datetime.now(IST)


def d(s):
    return s if isinstance(s, dt.date) else dt.date.fromisoformat(str(s)[:10])


_cal_cache = {}


def is_trading_day(day):
    day = d(day)
    if day.weekday() >= 5:
        return False
    extra = {x.strip() for x in os.environ.get("EXTRA_HOLIDAYS", "").split(",") if x.strip()}
    if day.isoformat() in extra:
        return False
    try:
        if day.year not in _cal_cache:
            _cal_cache[day.year] = (holidays.NSE(years=[day.year]) if hasattr(holidays, "NSE")
                                    else holidays.financial_holidays("XNSE", years=[day.year]))
        return day not in _cal_cache[day.year]
    except Exception as e:
        print(f"holiday lookup failed ({e}); treating {day} as a trading day")
        return True


def prev_trading_day(day):
    day = d(day) - dt.timedelta(days=1)
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


def next_trading_day(day):
    day = d(day) + dt.timedelta(days=1)
    while not is_trading_day(day):
        day += dt.timedelta(days=1)
    return day


def sessions_between(a, b):
    """Trading days strictly after a, up to and including b."""
    a, b = d(a), d(b)
    out, x = [], a + dt.timedelta(days=1)
    while x <= b:
        if is_trading_day(x):
            out.append(x)
        x += dt.timedelta(days=1)
    return out


def weekly_cycle(today, expiries, seen):
    """-> dict(prev_exp, next_exp, i, n, is_expiry, prev_exp_guessed) for the weekly cycle containing today.
    expiries: Dhan's (future) expiry list; seen: cached expiries incl. past ones."""
    today = d(today)
    allx = sorted({d(x) for x in list(expiries) + list(seen)})
    nxt = next((x for x in allx if x >= today), None)
    if nxt is None:
        return None
    prev = [x for x in allx if x < today]
    guessed = False
    if prev:
        prev_exp = prev[-1]
    else:  # first run with an empty cache: assume last week's expiry, moved back over holidays
        prev_exp = nxt - dt.timedelta(days=7)
        while not is_trading_day(prev_exp):
            prev_exp -= dt.timedelta(days=1)
        guessed = True
    window = sessions_between(prev_exp, nxt)
    i = window.index(today) if today in window else -1
    return {"prev_exp": prev_exp.isoformat(), "next_exp": nxt.isoformat(), "i": i, "n": len(window),
            "is_expiry": today == nxt, "prev_exp_guessed": guessed, "window": [x.isoformat() for x in window]}


def monthly_expiries(expiries):
    """Last expiry of each calendar month in the list (= the NIFTY monthly contract)."""
    by_month = {}
    for x in sorted(d(e) for e in expiries):
        by_month[(x.year, x.month)] = x
    return [x.isoformat() for x in sorted(by_month.values())]


def dte(expiry, day):
    return (d(expiry) - d(day)).days
