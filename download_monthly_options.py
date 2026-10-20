"""
DhanHQ 60-minute NIFTY *monthly-expiry* options downloader (for 45 -> 21 DTE research).

Writes into data/raw/hourly_parquet/options_monthly/ (git-ignored):
  year=YYYY/month=MM/options.parquet     rolling monthly options, ATM +/-10 strikes, CE+PE,
                                         expiry_code 1 (current month) and 2 (next month)

Facts about this endpoint (probed 2026-10-11):
  - no daily interval; 60-minute bars start 09:15 (7 per session). 1-minute bars of the
    next-month contract are ~57% zero-volume, so hourly is the sensible resolution.
  - `strike_offset` is ROLLING ATM-relative, as in the weekly data: key legs by absolute `strike`.
  - next-month (code 2) has +/-10 strikes through 2025 but only +/-3 from Jan 2026.
  - about 5% of code 2 rows (0.1% of code 1) carry a wrong `strike`, often with a wrong `spot`
    (e.g. 37400 / 38433 with NIFTY at 18050), while `close` is the price of the labelled offset.
    They are stored as received; downstream keep only rows where
    strike == round(spot / 50) * 50 + 50 * strike_offset. The weekly 1-minute data has none.
  - the response carries no expiry date: derive it downstream (the monthly expiry is the last
    `daily_features.is_expiry` session of the calendar month).

Resumable: a past month that already has a file is skipped. The current month is always refetched.

Usage:
  python download_monthly_options.py                       # Jan 2021 -> today
  python download_monthly_options.py --start 2024-01       # from a given month
  python download_monthly_options.py --start 2023-06 --end 2023-06 --force   # redo one month
"""
import argparse
import calendar
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import polars as pl
import requests

from update_market_data import IST, ROOT, STRIKES, _offset, credentials, month_range, throttle

OUT_DIR = ROOT / "data" / "raw" / "hourly_parquet" / "options_monthly"
EXPIRY_CODES = (1, 2)
INTERVAL = 60
FIRST_MONTH = date(2021, 1, 1)
_throttle_lock = threading.Lock()


def fetch_series(headers, code, strike, drv, f_str, t_str):
    """One rolling series for one month. Returns (frame or None, ok): ok is False only on a hard failure."""
    payload = {"securityId": "13", "exchangeSegment": "NSE_FNO", "instrument": "OPTIDX",
               "expiryFlag": "MONTH", "expiryCode": code, "strike": strike, "drvOptionType": drv,
               "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
               "fromDate": f_str, "toDate": t_str, "interval": INTERVAL}
    for backoff in (2, 5, 15, 30):
        with _throttle_lock:        # request starts stay >= 0.35 s apart across worker threads
            throttle()
        try:
            r = requests.post("https://api.dhan.co/v2/charts/rollingoption", headers=headers, json=payload, timeout=30)
        except Exception as e:
            print(f"  code {code} {strike} {drv}: {type(e).__name__}", flush=True)
            time.sleep(backoff)
            continue
        if r.status_code == 200:
            sub = (r.json().get("data") or {}).get("ce" if drv == "CALL" else "pe")
            ts = (sub or {}).get("timestamp") or []
            if not ts:
                return None, True
            n = len(ts)
            return pl.DataFrame({
                "timestamp_sec": [int(x) for x in ts],
                "expiry_code": [code] * n,
                "strike_label": [strike] * n,
                "strike_offset": [_offset(strike)] * n,
                "option_type": ["CE" if drv == "CALL" else "PE"] * n,
                **{k: [float(x) for x in sub.get(k, [0.0] * n)]
                   for k in ("strike", "spot", "open", "high", "low", "close")},
                "volume": [int(x) for x in sub.get("volume", [0] * n)],
                "oi": [int(x) for x in sub.get("oi", [0] * n)],
                "iv": [float(x) for x in sub.get("iv", [0.0] * n)],
            }), True
        print(f"  code {code} {strike} {drv}: HTTP {r.status_code} {r.text[:120]}", flush=True)
        if r.status_code in (401, 403):
            raise SystemExit("Dhan rejected the token: refresh DHAN_ACCESS_TOKEN in .env.")
        time.sleep(backoff)
    return None, False


def fetch_month(headers, f_str, t_str, workers):
    frames, failed = [], 0
    jobs = [(code, strike, drv) for code in EXPIRY_CODES for strike in STRIKES for drv in ("CALL", "PUT")]
    with ThreadPoolExecutor(max_workers=workers) as pool:   # each call takes ~3.5 s server-side
        for df, ok in pool.map(lambda j: fetch_series(headers, *j, f_str, t_str), jobs):
            failed += not ok
            if df is not None:
                frames.append(df)
    if not frames:
        return None, failed
    out = (pl.concat(frames)
             .with_columns((pl.from_epoch("timestamp_sec", time_unit="s") + IST).cast(pl.Datetime("us")).alias("timestamp"))
             .with_columns(pl.col("timestamp").dt.strftime("%Y-%m-%d").alias("date"),
                           pl.col("timestamp").dt.strftime("%H:%M").alias("time"))
             .drop("timestamp_sec")
             .unique(subset=["timestamp", "expiry_code", "strike", "option_type"], keep="last")
             .sort(["timestamp", "expiry_code", "strike", "option_type"]))
    return out, failed


def _month(s):
    y, m = s.split("-")
    return date(int(y), int(m), 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=_month, default=FIRST_MONTH, help="YYYY-MM (default 2021-01)")
    ap.add_argument("--end", type=_month, default=None, help="YYYY-MM (default: this month)")
    ap.add_argument("--force", action="store_true", help="refetch months that already have a file")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (default 4)")
    a = ap.parse_args()

    today = date.today()
    end = a.end or today
    headers = {"access-token": credentials()[1], "Content-Type": "application/json"}
    incomplete = []
    for y, m in month_range(a.start, end):
        out = OUT_DIR / f"year={y}" / f"month={m:02d}" / "options.parquet"
        if out.exists() and not a.force and (y, m) < (today.year, today.month):
            continue
        f_day = date(y, m, 1)
        t_day = min(date(y, m, calendar.monthrange(y, m)[1]), today)
        t0 = time.time()
        df, failed = fetch_month(headers, str(f_day), str(t_day), a.workers)
        if failed:
            incomplete.append(f"{y}-{m:02d}")
        if df is None:
            print(f"[MONTHLY] {y}-{m:02d}: no bars ({failed} series failed)", flush=True)
            continue
        if failed and out.exists():
            print(f"[MONTHLY] {y}-{m:02d}: {failed} series failed, keeping the existing file", flush=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        df.write_parquet(tmp, compression="zstd")
        os.replace(tmp, out)
        per_code = dict(df.group_by("expiry_code").len().sort("expiry_code").iter_rows())
        print(f"[MONTHLY] {y}-{m:02d}: {df['date'].min()} -> {df['date'].max()}, {len(df):,} rows "
              f"(code1 {per_code.get(1, 0):,}, code2 {per_code.get(2, 0):,}), {failed} series failed "
              f"({time.time() - t0:.0f}s)", flush=True)
    if incomplete:
        print("Months with failed series, rerun with --force: " + " ".join(incomplete))


if __name__ == "__main__":
    main()
