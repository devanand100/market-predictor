"""
Flow Signal Backtester
Tests how well FII/DII participant positioning predicts next-day market direction.
Generates accuracy reports, signal correlations, and regime classification metrics.
"""
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROCESSED_DIR = Path(__file__).parent / "data" / "processed"
REPORTS_DIR = Path(__file__).parent / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def interpolate_score(value: float, breakpoints: list[tuple[float, float]]) -> float:
    """Linear interpolation between breakpoints: [(threshold, score), ...]"""
    if value <= breakpoints[0][0]:
        return breakpoints[0][1]
    if value >= breakpoints[-1][0]:
        return breakpoints[-1][1]
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= value <= x1:
            t = (value - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return 0.0


def compute_flow_composite_score(row: pd.Series) -> dict:
    """Compute the composite flow sentiment score from a single row of data."""
    scores = {}

    # Signal 1: FII Futures L/S Ratio (30%)
    ls = row.get("fii_ls_ratio", np.nan)
    if pd.notna(ls):
        scores["fii_ls_ratio"] = interpolate_score(ls, [
            (0.3, -100), (0.5, -70), (0.7, -40), (1.0, 0),
            (1.3, +30), (1.5, +50), (2.0, +75), (3.0, +100)
        ])

    # Signal 2: FII Net Change momentum (15%)
    net_chg = row.get("fii_fut_net_change", np.nan)
    if pd.notna(net_chg):
        scores["fii_net_change"] = np.clip(net_chg / 5000 * 100, -100, 100)

    # Signal 3: FII Options Sentiment (15%)
    opt_sent = row.get("fii_options_sentiment", np.nan)
    if pd.notna(opt_sent):
        scores["fii_options"] = np.clip(opt_sent * 200, -100, 100)

    # Signal 4: FII-Retail Divergence (15%)
    div = row.get("fii_client_divergence", np.nan)
    if pd.notna(div):
        scores["divergence"] = np.clip(div * 60, -100, 100)

    # Signal 5: Both Selling flag (10%)
    both = row.get("both_selling", 0)
    scores["dii_counter"] = -70 if both else +20

    # Signal 6: FII Net Z-score (10%) — contrarian at extremes
    zscore = row.get("fii_net_zscore_60d", np.nan)
    if pd.notna(zscore):
        if abs(zscore) > 2.0:
            scores["extreme"] = -np.sign(zscore) * 50
        else:
            scores["extreme"] = np.sign(zscore) * abs(zscore) * 25

    # Signal 7: VIX level (5%)
    vix = row.get("vix_close", np.nan)
    if pd.notna(vix):
        scores["vix"] = interpolate_score(vix, [
            (10, +40), (13, +25), (16, +10), (20, -15), (25, -50), (30, -80), (40, -100)
        ])

    weights = {
        "fii_ls_ratio": 0.30, "fii_net_change": 0.15, "fii_options": 0.15,
        "divergence": 0.15, "dii_counter": 0.10, "extreme": 0.10, "vix": 0.05,
    }

    valid_scores = {k: v for k, v in scores.items() if k in weights}
    if not valid_scores:
        return {"composite": np.nan, "regime": "UNKNOWN", "signals": {}}

    # Re-normalize weights for available signals
    total_weight = sum(weights[k] for k in valid_scores)
    composite = sum(valid_scores[k] * weights[k] / total_weight for k in valid_scores)

    # Classify regime
    if composite >= 60:
        regime = "STRONG_BULLISH"
    elif composite >= 25:
        regime = "MILD_BULLISH"
    elif composite >= -25:
        regime = "NEUTRAL"
    elif composite >= -60:
        regime = "MILD_BEARISH"
    else:
        regime = "STRONG_BEARISH"

    return {"composite": round(composite, 2), "regime": regime, "signals": valid_scores}


def run_backtest(dataset_path: str = None) -> pd.DataFrame:
    """Run the flow signal backtest against historical market returns."""
    path = Path(dataset_path) if dataset_path else PROCESSED_DIR / "backtesting_dataset.csv"
    if not path.exists():
        log.error(f"Dataset not found: {path}")
        log.info("Run the downloaders first:")
        log.info("  python nse_data_downloader.py --start 2022-01-01 --features")
        log.info("  python price_data_downloader.py --start 2022-01-01 --merge")
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["date"])
    log.info(f"Loaded dataset: {len(df)} rows, {df['date'].min()} → {df['date'].max()}")

    # Check if participant data exists
    if "fii_ls_ratio" not in df.columns:
        log.error("No participant data columns found. Run nse_data_downloader.py first.")
        return pd.DataFrame()

    has_participant = df["fii_ls_ratio"].notna().sum()
    log.info(f"Rows with participant data: {has_participant} / {len(df)}")

    # Compute flow scores for each row
    results = []
    for _, row in df.iterrows():
        score_data = compute_flow_composite_score(row)
        results.append({
            "date": row["date"],
            "nifty_close": row.get("close", np.nan),
            "return_1d": row.get("return_1d", np.nan),
            "next_return_1d": row.get("next_return_1d", np.nan),
            "next_direction": row.get("next_direction", None),
            "composite_score": score_data["composite"],
            "flow_regime": score_data["regime"],
            **{f"sig_{k}": v for k, v in score_data["signals"].items()},
        })

    bt = pd.DataFrame(results)
    bt = bt.dropna(subset=["composite_score", "next_return_1d"])
    log.info(f"Backtestable rows: {len(bt)}")

    if bt.empty:
        log.warning("No overlapping data between participant OI and price data.")
        return bt

    # ─── Analysis ────────────────────────────────────────────────────────
    log.info("\n" + "=" * 70)
    log.info("BACKTEST RESULTS: Flow Sentiment Score vs Next-Day Returns")
    log.info("=" * 70)

    # 1. Correlation Analysis
    corr = bt["composite_score"].corr(bt["next_return_1d"])
    log.info(f"\n[1] Correlation (Flow Score vs Next-Day Return): {corr:.4f}")

    # 2. Regime Accuracy — Average next-day return by predicted regime
    log.info("\n[2] Average Next-Day Return by Predicted Flow Regime:")
    regime_stats = bt.groupby("flow_regime")["next_return_1d"].agg(["mean", "median", "std", "count"])
    regime_stats["mean_bps"] = regime_stats["mean"] * 10000
    log.info(f"\n{regime_stats.to_string()}")

    # 3. Directional Accuracy
    bt["predicted_direction"] = np.where(bt["composite_score"] > 0, "UP", "DOWN")
    bt["actual_direction"] = np.where(bt["next_return_1d"] > 0, "UP", "DOWN")
    accuracy = (bt["predicted_direction"] == bt["actual_direction"]).mean()
    log.info(f"\n[3] Directional Accuracy (UP/DOWN): {accuracy:.2%}")

    # 4. Strong signal accuracy (only when |score| > 40)
    strong = bt[bt["composite_score"].abs() > 40]
    if len(strong) > 0:
        strong_acc = (strong["predicted_direction"] == strong["actual_direction"]).mean()
        log.info(f"    Strong Signal Accuracy (|score| > 40): {strong_acc:.2%} ({len(strong)} signals)")

    # 5. Quintile analysis
    log.info("\n[4] Return Quintiles by Flow Score:")
    bt["score_quintile"] = pd.qcut(bt["composite_score"], 5, labels=["Q1(Bear)", "Q2", "Q3(Neutral)", "Q4", "Q5(Bull)"], duplicates="drop")
    quintile_stats = bt.groupby("score_quintile")["next_return_1d"].agg(["mean", "count"])
    quintile_stats["mean_bps"] = quintile_stats["mean"] * 10000
    log.info(f"\n{quintile_stats.to_string()}")

    # 6. Monthly regime distribution
    log.info("\n[5] Monthly Regime Distribution:")
    bt["month"] = bt["date"].dt.to_period("M")
    monthly = bt.groupby(["month", "flow_regime"]).size().unstack(fill_value=0)
    log.info(f"\n{monthly.tail(12).to_string()}")

    # 7. Individual signal correlations
    log.info("\n[6] Individual Signal Correlations with Next-Day Return:")
    sig_cols = [c for c in bt.columns if c.startswith("sig_")]
    for col in sig_cols:
        sig_corr = bt[col].corr(bt["next_return_1d"])
        log.info(f"    {col:25s}: {sig_corr:+.4f}")

    # Save results
    report_path = REPORTS_DIR / "backtest_results.csv"
    bt.to_csv(report_path, index=False)
    log.info(f"\nFull results saved to {report_path}")

    # Save summary
    summary = {
        "date_range": f"{bt['date'].min()} → {bt['date'].max()}",
        "total_signals": len(bt),
        "correlation": round(corr, 4),
        "directional_accuracy": round(accuracy, 4),
        "strong_signal_accuracy": round(strong_acc, 4) if len(strong) > 0 else None,
        "strong_signal_count": len(strong),
    }
    summary_path = REPORTS_DIR / "backtest_summary.json"
    import json
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info(f"Summary saved to {summary_path}")

    return bt


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backtest flow sentiment signals")
    parser.add_argument("--dataset", default=None, help="Path to backtesting_dataset.csv")
    args = parser.parse_args()
    run_backtest(args.dataset)
