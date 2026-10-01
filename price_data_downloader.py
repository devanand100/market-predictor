"""
Nifty 50 & India VIX Historical Price Downloader.
Uses Yahoo Finance (yfinance) for reliable OHLCV data going back to 2010+.
Used to merge with participant OI data for backtesting signal accuracy.
"""
import argparse
import logging
from datetime import date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DATA_DIR = Path(__file__).parent / "data"
PROCESSED_DIR = DATA_DIR / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def download_nifty_history(start: str = "2020-01-01", end: str = None) -> pd.DataFrame:
    """Download Nifty 50 daily OHLCV from Yahoo Finance."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed. Run: pip install yfinance")
        raise

    end = end or date.today().isoformat()
    log.info(f"Downloading Nifty 50 (^NSEI): {start} → {end}")

    nifty = yf.download("^NSEI", start=start, end=end, progress=False)
    if nifty.empty:
        log.error("No Nifty data returned")
        return pd.DataFrame()

    # Flatten multi-level columns if present
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = [col[0] for col in nifty.columns]

    nifty = nifty.reset_index()
    nifty.columns = [c.lower().replace(" ", "_") for c in nifty.columns]
    nifty = nifty.rename(columns={"adj_close": "adj_close"})

    # Compute returns
    nifty["return_1d"] = nifty["close"].pct_change()
    nifty["return_3d"] = nifty["close"].pct_change(3)
    nifty["return_5d"] = nifty["close"].pct_change(5)

    # Classify next-day direction (for backtesting target)
    nifty["next_return_1d"] = nifty["return_1d"].shift(-1)
    nifty["next_direction"] = pd.cut(
        nifty["next_return_1d"],
        bins=[-float("inf"), -0.01, -0.003, 0.003, 0.01, float("inf")],
        labels=["STRONG_BEARISH", "MILD_BEARISH", "SIDEWAYS", "MILD_BULLISH", "STRONG_BULLISH"],
    )

    output = PROCESSED_DIR / "nifty50_daily.csv"
    nifty.to_csv(output, index=False)
    log.info(f"Saved Nifty 50 data to {output} ({len(nifty)} rows)")
    return nifty


def download_vix_history(start: str = "2020-01-01", end: str = None) -> pd.DataFrame:
    """Download India VIX daily data from Yahoo Finance."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed. Run: pip install yfinance")
        raise

    end = end or date.today().isoformat()
    log.info(f"Downloading India VIX (^INDIAVIX): {start} → {end}")

    vix = yf.download("^INDIAVIX", start=start, end=end, progress=False)
    if vix.empty:
        log.error("No VIX data returned")
        return pd.DataFrame()

    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = [col[0] for col in vix.columns]

    vix = vix.reset_index()
    vix.columns = [c.lower().replace(" ", "_") for c in vix.columns]
    vix = vix.rename(columns={"close": "vix_close"})

    vix["vix_change_pct"] = vix["vix_close"].pct_change() * 100
    vix["vix_rank_252"] = vix["vix_close"].rolling(252, min_periods=60).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1]
    )

    output = PROCESSED_DIR / "india_vix_daily.csv"
    vix.to_csv(output, index=False)
    log.info(f"Saved India VIX data to {output} ({len(vix)} rows)")
    return vix


def merge_all_data() -> pd.DataFrame:
    """Merge participant OI, Nifty prices, and VIX into a single backtesting dataset."""
    participant_path = PROCESSED_DIR / "participant_oi_features.csv"
    nifty_path = PROCESSED_DIR / "nifty50_daily.csv"
    vix_path = PROCESSED_DIR / "india_vix_daily.csv"

    dfs = {}
    for name, path in [("participant", participant_path), ("nifty", nifty_path), ("vix", vix_path)]:
        if path.exists():
            df = pd.read_csv(path, parse_dates=["date"])
            df["date"] = pd.to_datetime(df["date"]).dt.normalize()
            dfs[name] = df
            log.info(f"Loaded {name}: {len(df)} rows")
        else:
            log.warning(f"Missing: {path}")

    if "nifty" not in dfs:
        log.error("Nifty data required for merge")
        return pd.DataFrame()

    merged = dfs["nifty"]

    if "vix" in dfs:
        vix_cols = ["date", "vix_close", "vix_change_pct", "vix_rank_252"]
        vix_subset = dfs["vix"][[c for c in vix_cols if c in dfs["vix"].columns]]
        merged = merged.merge(vix_subset, on="date", how="left")

    if "participant" in dfs:
        merged = merged.merge(dfs["participant"], on="date", how="left")

    output = PROCESSED_DIR / "backtesting_dataset.csv"
    merged.to_csv(output, index=False)
    log.info(f"Saved merged backtesting dataset to {output} ({len(merged)} rows)")

    # Summary
    log.info(f"\n--- Dataset Summary ---")
    log.info(f"  Date range:  {merged['date'].min()} → {merged['date'].max()}")
    log.info(f"  Total rows:  {len(merged)}")
    log.info(f"  Columns:     {len(merged.columns)}")
    if "fii_ls_ratio" in merged.columns:
        log.info(f"  Participant data coverage: {merged['fii_ls_ratio'].notna().sum()} / {len(merged)} days")

    return merged


def main():
    parser = argparse.ArgumentParser(description="Download Nifty 50 & VIX historical data")
    parser.add_argument("--start", default="2022-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--merge", action="store_true", help="Merge all datasets after download")
    args = parser.parse_args()

    download_nifty_history(start=args.start, end=args.end)
    download_vix_history(start=args.start, end=args.end)

    if args.merge:
        merge_all_data()


if __name__ == "__main__":
    main()
