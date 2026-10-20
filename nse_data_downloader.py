"""
NSE Historical Data Downloader
Downloads participant-wise OI, FII/DII cash activity, and option chain data
from NSE India archives for backtesting market direction prediction models.
"""
import os
import io
import csv
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

import requests
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
RAW_DIR = DATA_DIR / "raw"
PARTICIPANT_OI_DIR = RAW_DIR / "participant_oi"
FII_STATS_DIR = RAW_DIR / "fii_stats"
PROCESSED_DIR = DATA_DIR / "processed"

for d in [PARTICIPANT_OI_DIR, FII_STATS_DIR, PROCESSED_DIR]:
    d.mkdir(parents=True, exist_ok=True)

NSE_BASE = "https://www.nseindia.com"
NSE_ARCHIVES = "https://nsearchives.nseindia.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


class NSESession:
    """Manages NSE session with automatic cookie refresh."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._last_cookie_refresh = 0
        self._cookie_ttl = 120  # refresh cookies every 2 minutes

    def _refresh_cookies(self):
        now = time.time()
        if now - self._last_cookie_refresh < self._cookie_ttl:
            return
        try:
            resp = self.session.get(NSE_BASE, timeout=10)
            resp.raise_for_status()
            self._last_cookie_refresh = time.time()
            log.debug("NSE session cookies refreshed")
        except Exception as e:
            log.warning(f"Cookie refresh failed: {e}")

    def get(self, url: str, retries: int = 3, delay: float = 2.0) -> Optional[requests.Response]:
        self._refresh_cookies()
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 403:
                    log.warning(f"403 on attempt {attempt}, refreshing cookies...")
                    self._last_cookie_refresh = 0
                    self._refresh_cookies()
                    time.sleep(delay * attempt)
                    continue
                if resp.status_code == 404:
                    return None  # date has no data (holiday/weekend)
                log.warning(f"HTTP {resp.status_code} on attempt {attempt} for {url}")
            except requests.exceptions.RequestException as e:
                log.warning(f"Request error attempt {attempt}: {e}")
            time.sleep(delay * attempt)
        log.error(f"Failed after {retries} retries: {url}")
        return None


def special_weekend_sessions() -> set[date]:
    """Saturday/Sunday trading sessions (budget days, DR drills) seen in daily_features."""
    path = PROCESSED_DIR / "daily_features.parquet"
    if not path.exists():
        return set()
    idx = pd.to_datetime(pd.read_parquet(path, columns=["close"]).index)
    return {d.date() for d in idx if d.weekday() >= 5}


def get_trading_dates(start: date, end: date) -> list[date]:
    """Generate weekday dates (plus known weekend special sessions) between start and end.
    Actual holiday filtering happens at download time (404 = skip)."""
    weekend = special_weekend_sessions()
    dates = []
    current = start
    while current <= end:
        if current.weekday() < 5 or current in weekend:
            dates.append(current)
        current += timedelta(days=1)
    return dates


# ─── Participant-Wise Open Interest ─────────────────────────────────────────

def download_participant_oi_csv(session: NSESession, d: date) -> Optional[Path]:
    """Download single day's participant-wise OI CSV from NSE archives."""
    fname = f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv"
    dest = PARTICIPANT_OI_DIR / fname
    if dest.exists():
        log.debug(f"Already exists: {fname}")
        return dest

    url = f"{NSE_ARCHIVES}/content/nsccl/{fname}"
    resp = session.get(url)
    if resp is None:
        return None

    content = resp.text.strip()
    if not content or "Client" not in content:
        log.warning(f"Invalid content for {fname}")
        return None

    dest.write_text(content, encoding="utf-8")
    log.info(f"Downloaded: {fname}")
    return dest


def parse_participant_oi_csv(filepath: Path) -> Optional[dict]:
    """Parse a single participant OI CSV into structured dict.

    NSE CSV format (after header rows):
    Client | FutIdxLong | FutIdxShort | FutStkLong | FutStkShort | OptIdxCELong | OptIdxCEShort | OptIdxPELong | OptIdxPEShort | OptStkCELong | OptStkCEShort | OptStkPELong | OptStkPEShort | TotalLongContracts | TotalShortContracts
    (repeated for DII, FII, Pro)
    """
    try:
        text = filepath.read_text(encoding="utf-8")
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

        # Find the header row containing "Client" or "FII"
        header_idx = None
        for i, line in enumerate(lines):
            if "Client" in line or "FII" in line:
                # Check if the previous line looks like a header
                if i > 0 and ("Future" in lines[i - 1] or "Option" in lines[i - 1]):
                    header_idx = i - 1
                    break
                elif "Long" in line and "Short" in line:
                    header_idx = i
                    break

        # Try parsing with pandas (handles various CSV quirks)
        df = pd.read_csv(io.StringIO(text), skiprows=range(header_idx) if header_idx else None)

        # Clean column names
        df.columns = [c.strip().replace("\r", "") for c in df.columns]

        # Find the participant column
        part_col = None
        for col in df.columns:
            if df[col].astype(str).str.contains("Client|DII|FII|Pro", case=False, na=False).any():
                part_col = col
                break

        if part_col is None:
            # Try first column
            part_col = df.columns[0]

        result = {}
        for _, row in df.iterrows():
            participant = str(row[part_col]).strip()
            if participant not in ("Client", "DII", "FII", "Pro"):
                continue

            # Extract numeric values, handling commas
            def clean_num(val):
                try:
                    return int(str(val).replace(",", "").strip())
                except (ValueError, TypeError):
                    return 0

            numeric_cols = [c for c in df.columns if c != part_col]
            values = [clean_num(row[c]) for c in numeric_cols]

            # Map based on expected column order (NSE standard)
            # FutIdx Long, FutIdx Short, FutStk Long, FutStk Short,
            # OptIdx CE Long, OptIdx CE Short, OptIdx PE Long, OptIdx PE Short,
            # OptStk CE Long, OptStk CE Short, OptStk PE Long, OptStk PE Short,
            # Total Long, Total Short
            if len(values) >= 14:
                result[participant.lower()] = {
                    "fut_idx_long": values[0],
                    "fut_idx_short": values[1],
                    "fut_stk_long": values[2],
                    "fut_stk_short": values[3],
                    "opt_idx_ce_long": values[4],
                    "opt_idx_ce_short": values[5],
                    "opt_idx_pe_long": values[6],
                    "opt_idx_pe_short": values[7],
                    "opt_stk_ce_long": values[8],
                    "opt_stk_ce_short": values[9],
                    "opt_stk_pe_long": values[10],
                    "opt_stk_pe_short": values[11],
                    "total_long": values[12],
                    "total_short": values[13],
                }
            elif len(values) >= 4:
                # Minimal fallback
                result[participant.lower()] = {
                    "fut_idx_long": values[0],
                    "fut_idx_short": values[1],
                    "total_long": values[-2] if len(values) >= 2 else values[0],
                    "total_short": values[-1] if len(values) >= 2 else values[1],
                }

        if not result:
            log.warning(f"No participant data parsed from {filepath.name}")
            return None

        # Compute derived signals
        for p in result:
            d_val = result[p]
            long = d_val.get("fut_idx_long", 0)
            short = d_val.get("fut_idx_short", 0)
            d_val["fut_idx_net"] = long - short
            d_val["fut_idx_ls_ratio"] = round(long / max(short, 1), 4)

        return result

    except Exception as e:
        log.error(f"Parse error {filepath.name}: {e}")
        return None


# ─── FII/DII Cash Market Activity ───────────────────────────────────────────

def download_fii_cash_data(session: NSESession, d: date) -> Optional[dict]:
    """Fetch FII/DII cash market buy/sell data from NSE API.
    NSE provides this via the institutional trading activity report."""
    # Try the NSE API endpoint for FII/DII data
    date_str = d.strftime("%d-%m-%Y")
    url = f"{NSE_BASE}/api/fiidiiTradeReact"

    resp = session.get(url)
    if resp is None:
        return None

    try:
        data = resp.json()
        result = {}
        for entry in data:
            category = entry.get("category", "").strip()
            if "FII" in category or "FPI" in category:
                result["fii_cash"] = {
                    "buy_value_cr": float(entry.get("buyValue", 0)),
                    "sell_value_cr": float(entry.get("sellValue", 0)),
                    "net_value_cr": float(entry.get("netValue", 0)),
                }
            elif "DII" in category:
                result["dii_cash"] = {
                    "buy_value_cr": float(entry.get("buyValue", 0)),
                    "sell_value_cr": float(entry.get("sellValue", 0)),
                    "net_value_cr": float(entry.get("netValue", 0)),
                }
        return result if result else None
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning(f"FII cash parse error for {d}: {e}")
        return None


# ─── Bulk Historical Download ────────────────────────────────────────────────

def download_historical_participant_oi(
    start_date: str = "2020-01-01",
    end_date: str = None,
    throttle: float = 1.5,
) -> pd.DataFrame:
    """Download and parse all participant-wise OI data for the date range.

    Args:
        start_date: YYYY-MM-DD format
        end_date: YYYY-MM-DD format (defaults to today)
        throttle: Seconds between requests (be respectful to NSE servers)

    Returns:
        DataFrame with daily participant positioning data
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else date.today()

    dates = get_trading_dates(start, end)
    log.info(f"Downloading participant OI: {start} → {end} ({len(dates)} potential trading days)")

    session = NSESession()
    records = []
    downloaded = 0
    skipped = 0
    failed = 0

    for i, d in enumerate(dates):
        filepath = download_participant_oi_csv(session, d)

        if filepath is None:
            skipped += 1
            # Only throttle on actual HTTP requests (not cached files)
            time.sleep(throttle)
            continue

        parsed = parse_participant_oi_csv(filepath)
        if parsed:
            record = {"date": d.isoformat()}
            for participant, values in parsed.items():
                for key, val in values.items():
                    record[f"{participant}_{key}"] = val
            records.append(record)
            downloaded += 1
        else:
            failed += 1

        if (i + 1) % 50 == 0:
            log.info(f"Progress: {i + 1}/{len(dates)} | Downloaded: {downloaded} | Skipped: {skipped} | Failed: {failed}")

        # Throttle between downloads (skip if file was cached)
        if not (PARTICIPANT_OI_DIR / f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv").exists():
            time.sleep(throttle)

    log.info(f"Complete: {downloaded} downloaded, {skipped} skipped (holidays/weekends), {failed} parse failures")

    if not records:
        log.warning("No records collected")
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # Save processed data
    output_path = PROCESSED_DIR / "participant_oi_history.csv"
    df.to_csv(output_path, index=False)
    log.info(f"Saved processed data to {output_path} ({len(df)} rows)")

    return df


# ─── Feature Engineering for Backtesting ─────────────────────────────────────

def compute_flow_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add derived flow features for backtesting from raw participant data."""
    if df.empty:
        return df

    out = df.copy()

    # FII Futures Index L/S Ratio
    out["fii_ls_ratio"] = out["fii_fut_idx_long"] / out["fii_fut_idx_short"].replace(0, np.nan)

    # Client Futures Index L/S Ratio
    out["client_ls_ratio"] = out["client_fut_idx_long"] / out["client_fut_idx_short"].replace(0, np.nan)

    # FII-Client Divergence (smart vs retail money)
    out["fii_client_divergence"] = out["fii_ls_ratio"] - out["client_ls_ratio"]

    # FII Net Index Futures Position
    out["fii_fut_net"] = out["fii_fut_idx_long"] - out["fii_fut_idx_short"]

    # FII Net Change (day-over-day momentum)
    out["fii_fut_net_change"] = out["fii_fut_net"].diff()

    # FII 5-day rolling net change
    out["fii_fut_net_change_5d"] = out["fii_fut_net"].diff(5)

    # FII Options Sentiment: (PE write - CE write) normalized
    pe_write = out.get("fii_opt_idx_pe_short", 0)
    ce_write = out.get("fii_opt_idx_ce_short", 0)
    total_opt = pe_write + ce_write
    out["fii_options_sentiment"] = (pe_write - ce_write) / total_opt.replace(0, np.nan)

    # DII Net Futures
    out["dii_fut_net"] = out.get("dii_fut_idx_long", 0) - out.get("dii_fut_idx_short", 0)

    # DII Counter-Flow flag (both FII and DII selling = genuine weakness)
    out["both_selling"] = ((out["fii_fut_net"] < 0) & (out["dii_fut_net"] < 0)).astype(int)

    # FII Net Position Z-score (60-day rolling)
    rolling_mean = out["fii_fut_net"].rolling(60, min_periods=20).mean()
    rolling_std = out["fii_fut_net"].rolling(60, min_periods=20).std()
    out["fii_net_zscore_60d"] = (out["fii_fut_net"] - rolling_mean) / rolling_std.replace(0, np.nan)

    # Client extreme positioning (retail contrarian signal)
    client_net = out.get("client_fut_idx_long", 0) - out.get("client_fut_idx_short", 0)
    client_mean = client_net.rolling(60, min_periods=20).mean()
    client_std = client_net.rolling(60, min_periods=20).std()
    out["client_net_zscore_60d"] = (client_net - client_mean) / client_std.replace(0, np.nan)

    return out


# ─── CLI Entry Point ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download historical NSE FII/DII participant-wise OI data for backtesting"
    )
    parser.add_argument(
        "--start", default="2021-01-01",
        help="Start date YYYY-MM-DD (default: 2021-01-01, start of the options data)"
    )
    parser.add_argument(
        "--end", default=None,
        help="End date YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--throttle", type=float, default=1.5,
        help="Seconds between HTTP requests (default: 1.5, be kind to NSE)"
    )
    parser.add_argument(
        "--features", action="store_true",
        help="Also compute derived flow features after download"
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("NSE Historical Participant OI Downloader")
    log.info("=" * 60)

    df = download_historical_participant_oi(
        start_date=args.start,
        end_date=args.end,
        throttle=args.throttle,
    )

    if args.features and not df.empty:
        log.info("Computing derived flow features...")
        df_features = compute_flow_features(df)
        features_path = PROCESSED_DIR / "participant_oi_features.csv"
        df_features.to_csv(features_path, index=False)
        log.info(f"Saved features to {features_path}")

        # Print summary stats
        if "fii_ls_ratio" in df_features.columns:
            log.info("\n--- FII L/S Ratio Summary ---")
            log.info(f"  Mean:   {df_features['fii_ls_ratio'].mean():.3f}")
            log.info(f"  Median: {df_features['fii_ls_ratio'].median():.3f}")
            log.info(f"  Min:    {df_features['fii_ls_ratio'].min():.3f}")
            log.info(f"  Max:    {df_features['fii_ls_ratio'].max():.3f}")
            log.info(f"  Std:    {df_features['fii_ls_ratio'].std():.3f}")

    log.info("Done.")


if __name__ == "__main__":
    main()
