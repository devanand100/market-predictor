"""
Incremental DhanHQ 1-minute data updater for this repo (adapted from
nifty-quant-condor/python_bot/minute_data_manager.py, download parts only).

Writes into data/raw/minute_parquet/ (git-ignored):
  nifty_spot_1min.parquet, india_vix_1min.parquet     monolithic spot / VIX (read by build_research_data.py, engine.py)
  spot|vix/year=YYYY/month=MM/*.parquet                Hive copies of the same
  options/year=YYYY/month=MM/options.parquet           rolling weekly options, ATM +/-10 strikes, CE+PE

Only new data is fetched: each series restarts from the month of its last stored bar
(so a partly downloaded month is completed) and runs to today. Merges dedupe by timestamp.

Needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env at the repo root.

Usage:
  python update_market_data.py                 # spot + VIX + options, then rebuild research data
  python update_market_data.py --no-options    # spot + VIX only
  python update_market_data.py --no-rebuild    # skip build_research_data + engine cache
"""
import argparse
import calendar
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
MINUTE_DIR = ROOT / "data" / "raw" / "minute_parquet"
SPOT_FILE = MINUTE_DIR / "nifty_spot_1min.parquet"
VIX_FILE = MINUTE_DIR / "india_vix_1min.parquet"
OPTIONS_DIR = MINUTE_DIR / "options"

MIN_REQUEST_INTERVAL_SEC = 0.35
STRIKES = ["ATM"] + [f"ATM+{i}" for i in range(1, 11)] + [f"ATM-{i}" for i in range(1, 11)]
IST = timedelta(hours=5, minutes=30)


def credentials():
    load_dotenv(ROOT / ".env")
    cid = os.getenv("DHAN_CLIENT_ID", "").strip()
    tok = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    if not cid or not tok:
        sys.exit("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing: add them to .env in the repo root.")
    return cid, tok


def dhan_client():
    from dhanhq import DhanContext, dhanhq
    return dhanhq(DhanContext(*credentials()))


def month_range(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


_last_req = 0.0


def throttle():
    global _last_req
    wait = MIN_REQUEST_INTERVAL_SEC - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.time()


# ---------------------------------------------------------------- spot / VIX
def fetch_index_minutes(dhan, security_id, label, start, end, chunk_days=25):
    """1-minute OHLCV for an index between two dates, in rate-limited chunks."""
    cols = {k: [] for k in ("timestamp_raw", "open", "high", "low", "close", "volume")}
    cur = start
    while cur <= end:
        c_to = min(cur + timedelta(days=chunk_days), end)
        for attempt, backoff in enumerate((1, 2, 4)):
            throttle()
            try:
                r = dhan.intraday_minute_data(security_id=security_id, exchange_segment=dhan.INDEX,
                                              instrument_type="INDEX", from_date=str(cur), to_date=str(c_to))
                if r.get("status") == "success" and "data" in r:
                    d = r["data"]
                    ts = d.get("timestamp", [])
                    cols["timestamp_raw"] += ts
                    for k in ("open", "high", "low", "close"):
                        cols[k] += [float(x) for x in d.get(k, [])]
                    cols["volume"] += [int(x) for x in d.get("volume", [0] * len(ts))]
                    break
                print(f"  {label} {cur}->{c_to}: {r.get('remarks') or r.get('error')}")
            except Exception as e:
                print(f"  {label} {cur}->{c_to}: {e}")
            time.sleep(backoff)
        print(f"[{label}] {cur} -> {c_to} | bars so far {len(cols['timestamp_raw']):,}", flush=True)
        cur = c_to + timedelta(days=1)

    if not cols["timestamp_raw"]:
        return None
    df = pl.DataFrame(cols).unique(subset=["timestamp_raw"]).sort("timestamp_raw")
    return (df.with_columns((pl.from_epoch("timestamp_raw", time_unit="s") + IST).cast(pl.Datetime("us")).alias("timestamp"))
              .with_columns(pl.col("timestamp").dt.strftime("%Y-%m-%d").alias("date"),
                            pl.col("timestamp").dt.strftime("%H:%M").alias("time"))
              .drop("timestamp_raw"))


def update_index(dhan, security_id, label, path, hive_dir, hive_name):
    old = pl.read_parquet(path)
    last = datetime.strptime(old["date"].max(), "%Y-%m-%d").date()
    today = date.today()
    if last >= today:
        print(f"[{label}] up to date ({last})")
        return
    new = fetch_index_minutes(dhan, security_id, label, last, today)
    if new is None:
        print(f"[{label}] no new bars")
        return
    merged = pl.concat([old, new.select(old.columns)]).unique(subset=["timestamp"], keep="last").sort("timestamp")
    tmp = path.with_suffix(".tmp")
    merged.write_parquet(tmp, compression="zstd")
    os.replace(tmp, path)
    print(f"[{label}] {old['date'].max()} -> {merged['date'].max()} (+{len(merged) - len(old):,} bars)")

    # Rewrite the Hive partitions for every month touched
    for y, m in month_range(last, today):
        part = merged.filter((pl.col("timestamp").dt.year() == y) & (pl.col("timestamp").dt.month() == m))
        if len(part):
            d = hive_dir / f"year={y}" / f"month={m:02d}"
            d.mkdir(parents=True, exist_ok=True)
            part.write_parquet(d / hive_name, compression="zstd")


# ---------------------------------------------------------------- options
def _offset(label):
    return 0 if label == "ATM" else int(label[3:])


def fetch_option_month(token, f_str, t_str):
    headers = {"access-token": token, "Content-Type": "application/json"}
    frames = []
    for strike in STRIKES:
        for drv in ("CALL", "PUT"):
            payload = {"securityId": "13", "exchangeSegment": "NSE_FNO", "instrument": "OPTIDX",
                       "expiryFlag": "WEEK", "expiryCode": 1, "strike": strike, "drvOptionType": drv,
                       "requiredData": ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"],
                       "fromDate": f_str, "toDate": t_str, "interval": 1}
            for backoff in (1, 2, 4):
                throttle()
                try:
                    r = requests.post("https://api.dhan.co/v2/charts/rollingoption",
                                      headers=headers, json=payload, timeout=15)
                except Exception as e:
                    print(f"  {strike} {drv}: {e}")
                    time.sleep(backoff)
                    continue
                if r.status_code == 200:
                    sub = (r.json().get("data") or {}).get("ce" if drv == "CALL" else "pe")
                    ts = (sub or {}).get("timestamp") or []
                    if ts:
                        n = len(ts)
                        frames.append(pl.DataFrame({
                            "timestamp_sec": [int(x) for x in ts],
                            "strike_label": [strike] * n,
                            "strike_offset": [_offset(strike)] * n,
                            "option_type": ["CE" if drv == "CALL" else "PE"] * n,
                            **{k: [float(x) for x in sub.get(k, [0.0] * n)]
                               for k in ("strike", "spot", "open", "high", "low", "close")},
                            "volume": [int(x) for x in sub.get("volume", [0] * n)],
                            "oi": [int(x) for x in sub.get("oi", [0] * n)],
                            "iv": [float(x) for x in sub.get("iv", [0.0] * n)],
                        }))
                    break
                print(f"  {strike} {drv}: HTTP {r.status_code} {r.text[:120]}")
                if r.status_code in (401, 403):
                    sys.exit("Dhan rejected the token: refresh DHAN_ACCESS_TOKEN in .env.")
                time.sleep(backoff)
    if not frames:
        return None
    return (pl.concat(frames)
              .with_columns((pl.from_epoch("timestamp_sec", time_unit="s") + IST).cast(pl.Datetime("us")).alias("timestamp"))
              .with_columns(pl.col("timestamp").dt.strftime("%Y-%m-%d").alias("date"),
                            pl.col("timestamp").dt.strftime("%H:%M").alias("time"))
              .drop("timestamp_sec"))


def update_options(token):
    files = sorted(OPTIONS_DIR.glob("year=*/month=*/options.parquet"))
    last = datetime.strptime(pl.read_parquet(files[-1])["date"].max(), "%Y-%m-%d").date()
    today = date.today()
    print(f"[OPTIONS] last stored day {last}; fetching through {today}")
    for y, m in month_range(last, today):
        f_day = last if (y, m) == (last.year, last.month) else date(y, m, 1)
        t_day = min(date(y, m, calendar.monthrange(y, m)[1]), today)
        t0 = time.time()
        new = fetch_option_month(token, str(f_day), str(t_day))
        out = OPTIONS_DIR / f"year={y}" / f"month={m:02d}" / "options.parquet"
        if new is None:
            print(f"[OPTIONS] {y}-{m:02d}: no bars")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            old = pl.read_parquet(out)
            new = pl.concat([old, new.select(old.columns)])
        new = new.unique(subset=["timestamp", "strike", "option_type"], keep="last").sort(["timestamp", "strike", "option_type"])
        tmp = out.with_suffix(".tmp")
        new.write_parquet(tmp, compression="zstd")
        os.replace(tmp, out)
        print(f"[OPTIONS] {y}-{m:02d}: {new['date'].min()} -> {new['date'].max()}, "
              f"{len(new):,} rows ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-options", action="store_true")
    ap.add_argument("--no-rebuild", action="store_true")
    a = ap.parse_args()

    dhan = dhan_client()
    update_index(dhan, "13", "NIFTY_SPOT", SPOT_FILE, MINUTE_DIR / "spot", "spot.parquet")
    update_index(dhan, "21", "INDIA_VIX", VIX_FILE, MINUTE_DIR / "vix", "vix.parquet")
    if not a.no_options:
        update_options(credentials()[1])
    if not a.no_rebuild:
        subprocess.run([sys.executable, "build_research_data.py", "--rebuild"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-c", "import engine; engine.load_days(rebuild=True)"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
