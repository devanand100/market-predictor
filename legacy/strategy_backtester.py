"""
Institutional-Grade Options Strategy Backtester

Uses actual 1-minute OHLCV + OI + IV data from DhanHQ Parquet archives.
Strategies tested:
  1. Weekly Short Strangle (sell OTM CE + OTM PE, SL-based exit)
  2. Weekly Iron Condor (short strangle + long wings for defined risk)
  3. VIX-filtered Short Strangle (only enter when VIX > threshold)
  4. FII-Flow-filtered Short Strangle (skip when FII strongly bearish)

All strategies use:
  - Actual option premiums from minute data (not Black-Scholes theoretical)
  - Slippage model: entry at close+spread, exit at close+spread
  - Full SEBI statutory cost deduction
  - Weekly expiry cycle (Thursday close → next Thursday/Wednesday)
  - Intraday SL monitoring at 1-min granularity
"""
import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from cost_model import (
    get_lot_size,
    compute_round_trip_costs,
    BROKERAGE_PER_ORDER,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

PARQUET_BASE = Path(r"C:\Users\Admin\Documents\Trading Bot\minute_parquet")
FII_DATA_PATH = Path(r"C:\Users\Admin\Documents\market-predictor\data\processed\participant_oi_features.csv")
REPORTS_DIR = Path(__file__).parent / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Slippage: assume entry/exit at 0.5% worse than mid price
SLIPPAGE_PCT = 0.005

# Entry time: 09:20 (5 min after open, let volatility settle)
ENTRY_TIME = "09:20"
# Latest entry time (skip if no data before this)
LATEST_ENTRY_TIME = "10:00"
# Exit time on expiry day: 15:15 (15 min before close to avoid pin risk)
EXPIRY_EXIT_TIME = "15:15"


# ─── Data Loading ────────────────────────────────────────────────────────────

def load_spot_daily(start_year: int = 2021) -> pd.DataFrame:
    """Load spot data, resample to daily OHLCV."""
    log.info("Loading spot minute data...")
    spot = pd.read_parquet(PARQUET_BASE / "spot")
    spot["timestamp"] = pd.to_datetime(spot["timestamp"])
    spot = spot[spot["timestamp"].dt.year >= start_year].copy()
    spot = spot.set_index("timestamp").sort_index()

    daily = spot.resample("D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    log.info(f"Spot daily: {len(daily)} days, {daily.index.min()} → {daily.index.max()}")
    return daily


def load_vix_daily(start_year: int = 2021) -> pd.DataFrame:
    """Load VIX data, resample to daily."""
    log.info("Loading VIX minute data...")
    vix = pd.read_parquet(PARQUET_BASE / "vix")
    vix["timestamp"] = pd.to_datetime(vix["timestamp"])
    vix = vix[vix["timestamp"].dt.year >= start_year].copy()
    vix = vix.set_index("timestamp").sort_index()

    daily = vix.resample("D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    daily = daily.rename(columns={"open": "vix_open", "high": "vix_high",
                                   "low": "vix_low", "close": "vix_close"})
    log.info(f"VIX daily: {len(daily)} days")
    return daily


def load_options_for_month(year: int, month: int) -> Optional[pd.DataFrame]:
    """Load options data for a specific year/month."""
    path = PARQUET_BASE / "options" / f"year={year}" / f"month={month:02d}" / "options.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def load_fii_data() -> Optional[pd.DataFrame]:
    """Load FII/DII participant data."""
    if not FII_DATA_PATH.exists():
        log.warning("FII data not found, skipping flow filter")
        return None
    df = pd.read_csv(FII_DATA_PATH, parse_dates=["date"])
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    log.info(f"FII data loaded: {len(df)} days")
    return df


# ─── Expiry Calendar ─────────────────────────────────────────────────────────

def build_expiry_calendar(spot_daily: pd.DataFrame) -> list[dict]:
    """Build weekly expiry dates from actual trading days.
    NSE Nifty weekly expiry: Thursday (shifted to Wednesday if Thursday is holiday).
    """
    trading_days = sorted(spot_daily.index.normalize().unique())
    td_set = set(trading_days)

    expiries = []
    for td in trading_days:
        dow = td.dayofweek
        # Thursday = 3
        if dow == 3 and td in td_set:
            expiries.append(td)
        # Wednesday = 2, check if next day (Thursday) is NOT a trading day
        elif dow == 2:
            next_thu = td + pd.Timedelta(days=1)
            if next_thu not in td_set:
                expiries.append(td)

    log.info(f"Expiry calendar: {len(expiries)} weekly expiries detected")
    return expiries


def build_weekly_cycles(expiries: list, trading_days_set: set) -> list[dict]:
    """Build entry→exit cycles for weekly options selling.
    Entry: Previous expiry day (Thursday/Wednesday) after market settles.
    Exit: Current expiry day before close.
    """
    cycles = []
    for i in range(1, len(expiries)):
        entry_date = expiries[i - 1]
        exit_date = expiries[i]

        # Get trading days in this cycle
        cycle_days = sorted([d for d in trading_days_set
                            if entry_date <= d <= exit_date])

        if len(cycle_days) < 2:
            continue

        cycles.append({
            "entry_date": entry_date,
            "exit_date": exit_date,
            "trading_days": cycle_days,
            "num_days": len(cycle_days),
        })

    log.info(f"Weekly cycles: {len(cycles)}")
    return cycles


# ─── Strategy: Short Strangle ────────────────────────────────────────────────

def get_option_price_at_time(options_df: pd.DataFrame, date_str: str, time_str: str,
                              strike_offset: int, option_type: str) -> Optional[dict]:
    """Get option price at a specific date/time from minute data."""
    mask = (
        (options_df["date"] == date_str) &
        (options_df["time"] == time_str) &
        (options_df["strike_offset"] == strike_offset) &
        (options_df["option_type"] == option_type)
    )
    rows = options_df[mask]
    if rows.empty:
        return None

    row = rows.iloc[0]
    return {
        "close": float(row["close"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "open": float(row["open"]),
        "strike": float(row["strike"]),
        "spot": float(row["spot"]),
        "iv": float(row["iv"]),
        "oi": int(row["oi"]),
        "volume": int(row["volume"]),
    }


def get_intraday_high(options_df: pd.DataFrame, date_str: str,
                       strike_offset: int, option_type: str,
                       from_time: str = "09:15", to_time: str = "15:29") -> float:
    """Get the intraday high of an option between two times."""
    mask = (
        (options_df["date"] == date_str) &
        (options_df["time"] >= from_time) &
        (options_df["time"] <= to_time) &
        (options_df["strike_offset"] == strike_offset) &
        (options_df["option_type"] == option_type)
    )
    rows = options_df[mask]
    if rows.empty:
        return 0.0
    return float(rows["high"].max())


def simulate_intraday_sl_hit(options_df: pd.DataFrame, date_str: str,
                              strike_offset: int, option_type: str,
                              sl_price: float, from_time: str = "09:15") -> Optional[str]:
    """Check if SL was hit intraday, return the time it was hit."""
    mask = (
        (options_df["date"] == date_str) &
        (options_df["time"] >= from_time) &
        (options_df["strike_offset"] == strike_offset) &
        (options_df["option_type"] == option_type)
    )
    rows = options_df[mask].sort_values("time")
    for _, row in rows.iterrows():
        if float(row["high"]) >= sl_price:
            return str(row["time"])
    return None


def run_short_strangle_backtest(
    ce_offset: int = 5,       # ATM+5 for CE sell (500 points OTM approx)
    pe_offset: int = -5,      # ATM-5 for PE sell (500 points OTM approx)
    sl_multiplier: float = 1.5,  # SL at 1.5x entry premium
    num_lots: int = 1,
    start_year: int = 2022,
    end_year: int = 2026,
    vix_min: float = 0.0,     # Minimum VIX to enter (0 = no filter)
    vix_max: float = 100.0,   # Maximum VIX to enter (100 = no filter)
    fii_filter: bool = False,  # Use FII flow filter
    strategy_name: str = "Short Strangle",
    # Iron Condor params (if > 0, buy wings at this offset beyond short strikes)
    wing_offset: int = 0,     # 0 = no wings (pure strangle), e.g. 3 = buy wings 3 strikes beyond
) -> dict:
    """Run the short strangle/iron condor backtest with full cost accounting."""

    log.info(f"\n{'='*70}")
    log.info(f"BACKTESTING: {strategy_name}")
    log.info(f"CE: ATM+{ce_offset}, PE: ATM{pe_offset}, SL: {sl_multiplier}x")
    log.info(f"Period: {start_year}-{end_year}, Lots: {num_lots}")
    if vix_min > 0 or vix_max < 100:
        log.info(f"VIX filter: {vix_min} - {vix_max}")
    if fii_filter:
        log.info("FII flow filter: ENABLED")
    if wing_offset > 0:
        log.info(f"Wing protection: +/- {wing_offset} strikes beyond short legs")
    log.info(f"{'='*70}")

    # Load data
    spot_daily = load_spot_daily(start_year)
    vix_daily = load_vix_daily(start_year)
    fii_data = load_fii_data() if fii_filter else None

    # Build expiry calendar
    expiries = build_expiry_calendar(spot_daily)
    trading_days_set = set(spot_daily.index.normalize())
    cycles = build_weekly_cycles(expiries, trading_days_set)

    # Filter to date range
    cycles = [c for c in cycles
              if c["entry_date"].year >= start_year and c["exit_date"].year <= end_year]

    trades = []
    total_costs = 0.0
    skipped_vix = 0
    skipped_fii = 0
    skipped_data = 0

    for ci, cycle in enumerate(cycles):
        entry_date = cycle["entry_date"]
        exit_date = cycle["exit_date"]
        entry_date_str = entry_date.strftime("%Y-%m-%d")
        exit_date_str = exit_date.strftime("%Y-%m-%d")

        # ─── VIX Filter ─────────────────────────────────────────────────
        vix_at_entry = None
        if entry_date in vix_daily.index:
            vix_at_entry = float(vix_daily.loc[entry_date, "vix_close"])
        elif entry_date.normalize() in vix_daily.index:
            vix_at_entry = float(vix_daily.loc[entry_date.normalize(), "vix_close"])

        if vix_at_entry is not None:
            if vix_at_entry < vix_min or vix_at_entry > vix_max:
                skipped_vix += 1
                continue

        # ─── FII Filter ─────────────────────────────────────────────────
        if fii_filter and fii_data is not None:
            fii_row = fii_data[fii_data["date"] == entry_date.normalize()]
            if not fii_row.empty:
                fii_net = fii_row.iloc[0].get("fii_fut_net", 0)
                fii_net_chg = fii_row.iloc[0].get("fii_fut_net_change", 0)
                # Skip when FII is heavily net short AND increasing shorts
                if fii_net < -200000 and fii_net_chg < -10000:
                    skipped_fii += 1
                    continue

        # ─── Load Options Data ───────────────────────────────────────────
        year = entry_date.year
        month = entry_date.month
        opts = load_options_for_month(year, month)

        # Also load next month if exit is in different month
        if exit_date.month != month or exit_date.year != year:
            opts2 = load_options_for_month(exit_date.year, exit_date.month)
            if opts2 is not None:
                if opts is not None:
                    opts = pd.concat([opts, opts2], ignore_index=True)
                else:
                    opts = opts2

        if opts is None:
            skipped_data += 1
            continue

        # ─── Entry ───────────────────────────────────────────────────────
        ce_entry = get_option_price_at_time(opts, entry_date_str, ENTRY_TIME, ce_offset, "CE")
        pe_entry = get_option_price_at_time(opts, entry_date_str, ENTRY_TIME, pe_offset, "PE")

        if ce_entry is None or pe_entry is None:
            # Try slightly later times
            for t in ["09:21", "09:25", "09:30", "09:45", "10:00"]:
                if ce_entry is None:
                    ce_entry = get_option_price_at_time(opts, entry_date_str, t, ce_offset, "CE")
                if pe_entry is None:
                    pe_entry = get_option_price_at_time(opts, entry_date_str, t, pe_offset, "PE")
                if ce_entry is not None and pe_entry is not None:
                    break

        if ce_entry is None or pe_entry is None:
            skipped_data += 1
            continue

        # Skip if premiums are too low (< ₹5) — illiquid
        if ce_entry["close"] < 5 or pe_entry["close"] < 5:
            skipped_data += 1
            continue

        lot_size = get_lot_size(entry_date_str)

        # Entry premiums with slippage (selling: get slightly less)
        ce_entry_prem = ce_entry["close"] * (1 - SLIPPAGE_PCT)
        pe_entry_prem = pe_entry["close"] * (1 - SLIPPAGE_PCT)
        total_credit = ce_entry_prem + pe_entry_prem

        # Wing costs (for Iron Condor)
        wing_debit = 0.0
        ce_wing_entry_prem = 0.0
        pe_wing_entry_prem = 0.0
        if wing_offset > 0:
            ce_wing_offset = ce_offset + wing_offset
            pe_wing_offset = pe_offset - wing_offset
            ce_wing = get_option_price_at_time(opts, entry_date_str, ENTRY_TIME, ce_wing_offset, "CE")
            pe_wing = get_option_price_at_time(opts, entry_date_str, ENTRY_TIME, pe_wing_offset, "PE")
            if ce_wing and pe_wing:
                ce_wing_entry_prem = ce_wing["close"] * (1 + SLIPPAGE_PCT)  # buying: pay more
                pe_wing_entry_prem = pe_wing["close"] * (1 + SLIPPAGE_PCT)
                wing_debit = ce_wing_entry_prem + pe_wing_entry_prem
            else:
                skipped_data += 1
                continue

        net_credit = total_credit - wing_debit

        # SL prices
        ce_sl = ce_entry_prem * sl_multiplier
        pe_sl = pe_entry_prem * sl_multiplier

        # ─── Monitor Intraday SL ─────────────────────────────────────────
        ce_exit_prem = None
        pe_exit_prem = None
        ce_sl_hit = False
        pe_sl_hit = False
        exit_reason = "EXPIRY"
        actual_exit_date = exit_date_str

        for day in cycle["trading_days"]:
            day_str = day.strftime("%Y-%m-%d")
            is_expiry = (day == exit_date)

            # Check day's data exists
            day_opts = opts[opts["date"] == day_str]
            if day_opts.empty:
                continue

            start_time = ENTRY_TIME if day == entry_date else "09:15"
            end_time = EXPIRY_EXIT_TIME if is_expiry else "15:29"

            # Check CE SL
            if not ce_sl_hit:
                sl_time = simulate_intraday_sl_hit(opts, day_str, ce_offset, "CE",
                                                    ce_sl, start_time)
                if sl_time:
                    ce_sl_hit = True
                    # Exit at SL price + slippage (buying back: pay more)
                    ce_exit_prem = ce_sl * (1 + SLIPPAGE_PCT)
                    exit_reason = f"CE_SL@{day_str} {sl_time}"

            # Check PE SL
            if not pe_sl_hit:
                sl_time = simulate_intraday_sl_hit(opts, day_str, pe_offset, "PE",
                                                    pe_sl, start_time)
                if sl_time:
                    pe_sl_hit = True
                    pe_exit_prem = pe_sl * (1 + SLIPPAGE_PCT)
                    if "PE_SL" not in exit_reason and "CE_SL" not in exit_reason:
                        exit_reason = f"PE_SL@{day_str} {sl_time}"
                    elif "CE_SL" in exit_reason:
                        exit_reason = f"BOTH_SL"

            # If both SLs hit, exit immediately
            if ce_sl_hit and pe_sl_hit:
                actual_exit_date = day_str
                break

            # On expiry day, close remaining legs
            if is_expiry:
                actual_exit_date = day_str
                if not ce_sl_hit:
                    ce_close = get_option_price_at_time(opts, day_str, EXPIRY_EXIT_TIME,
                                                         ce_offset, "CE")
                    if ce_close:
                        ce_exit_prem = ce_close["close"] * (1 + SLIPPAGE_PCT)
                    else:
                        # Try last available time
                        for t in ["15:10", "15:00", "14:30"]:
                            ce_close = get_option_price_at_time(opts, day_str, t, ce_offset, "CE")
                            if ce_close:
                                ce_exit_prem = ce_close["close"] * (1 + SLIPPAGE_PCT)
                                break
                        if ce_exit_prem is None:
                            ce_exit_prem = 0.5  # Near-zero expiry

                if not pe_sl_hit:
                    pe_close = get_option_price_at_time(opts, day_str, EXPIRY_EXIT_TIME,
                                                         pe_offset, "PE")
                    if pe_close:
                        pe_exit_prem = pe_close["close"] * (1 + SLIPPAGE_PCT)
                    else:
                        for t in ["15:10", "15:00", "14:30"]:
                            pe_close = get_option_price_at_time(opts, day_str, t, pe_offset, "PE")
                            if pe_close:
                                pe_exit_prem = pe_close["close"] * (1 + SLIPPAGE_PCT)
                                break
                        if pe_exit_prem is None:
                            pe_exit_prem = 0.5

        # Handle missing exit prices
        if ce_exit_prem is None:
            ce_exit_prem = 0.5  # Expired worthless
        if pe_exit_prem is None:
            pe_exit_prem = 0.5

        # ─── Wing Exit (Iron Condor) ─────────────────────────────────────
        ce_wing_exit_prem = 0.0
        pe_wing_exit_prem = 0.0
        if wing_offset > 0:
            # Wings expire worthless or close near zero
            ce_wing_close = get_option_price_at_time(opts, actual_exit_date, EXPIRY_EXIT_TIME,
                                                       ce_offset + wing_offset, "CE")
            pe_wing_close = get_option_price_at_time(opts, actual_exit_date, EXPIRY_EXIT_TIME,
                                                       pe_offset - wing_offset, "PE")
            ce_wing_exit_prem = (ce_wing_close["close"] * (1 - SLIPPAGE_PCT)) if ce_wing_close else 0.0
            pe_wing_exit_prem = (pe_wing_close["close"] * (1 - SLIPPAGE_PCT)) if pe_wing_close else 0.0

        # ─── P&L Calculation ─────────────────────────────────────────────
        # Short strangle P&L per unit = (entry credit - exit debit)
        ce_pnl_per_unit = ce_entry_prem - ce_exit_prem
        pe_pnl_per_unit = pe_entry_prem - pe_exit_prem
        wing_pnl_per_unit = (ce_wing_exit_prem + pe_wing_exit_prem) - (ce_wing_entry_prem + pe_wing_entry_prem)

        gross_pnl_per_unit = ce_pnl_per_unit + pe_pnl_per_unit + wing_pnl_per_unit
        gross_pnl = gross_pnl_per_unit * lot_size * num_lots

        # ─── Cost Calculation ────────────────────────────────────────────
        # Short legs costs
        ce_costs = compute_round_trip_costs(ce_entry_prem, ce_exit_prem, lot_size,
                                             num_lots, entry_date_str, is_short=True)
        pe_costs = compute_round_trip_costs(pe_entry_prem, pe_exit_prem, lot_size,
                                             num_lots, entry_date_str, is_short=True)
        total_cost = ce_costs["total_round_trip"] + pe_costs["total_round_trip"]

        # Wing costs (if Iron Condor)
        if wing_offset > 0:
            ce_wing_costs = compute_round_trip_costs(ce_wing_entry_prem, ce_wing_exit_prem,
                                                       lot_size, num_lots, entry_date_str, is_short=False)
            pe_wing_costs = compute_round_trip_costs(pe_wing_entry_prem, pe_wing_exit_prem,
                                                       lot_size, num_lots, entry_date_str, is_short=False)
            total_cost += ce_wing_costs["total_round_trip"] + pe_wing_costs["total_round_trip"]

        net_pnl = gross_pnl - total_cost
        total_costs += total_cost

        trades.append({
            "entry_date": entry_date_str,
            "exit_date": actual_exit_date,
            "spot_at_entry": ce_entry.get("spot", 0),
            "vix_at_entry": vix_at_entry,
            "ce_strike": ce_entry["strike"],
            "pe_strike": pe_entry["strike"],
            "ce_entry_prem": round(ce_entry_prem, 2),
            "pe_entry_prem": round(pe_entry_prem, 2),
            "total_credit": round(total_credit, 2),
            "wing_debit": round(wing_debit, 2),
            "net_credit": round(net_credit, 2),
            "ce_exit_prem": round(ce_exit_prem, 2),
            "pe_exit_prem": round(pe_exit_prem, 2),
            "ce_sl_hit": ce_sl_hit,
            "pe_sl_hit": pe_sl_hit,
            "exit_reason": exit_reason,
            "gross_pnl": round(gross_pnl, 2),
            "total_cost": round(total_cost, 2),
            "net_pnl": round(net_pnl, 2),
            "lot_size": lot_size,
            "num_lots": num_lots,
            "ce_iv": ce_entry.get("iv", 0),
            "pe_iv": pe_entry.get("iv", 0),
        })

        if (ci + 1) % 50 == 0:
            log.info(f"Progress: {ci+1}/{len(cycles)} cycles processed")

    log.info(f"Skipped: VIX={skipped_vix}, FII={skipped_fii}, Data={skipped_data}")

    # ─── Results Analysis ────────────────────────────────────────────────
    if not trades:
        log.error("No trades executed")
        return {}

    df = pd.DataFrame(trades)
    df["cum_pnl"] = df["net_pnl"].cumsum()
    df["entry_date_dt"] = pd.to_datetime(df["entry_date"])

    results = analyze_results(df, strategy_name, num_lots, total_costs)
    results["trades_df"] = df
    results["strategy_name"] = strategy_name

    return results


def analyze_results(df: pd.DataFrame, strategy_name: str, num_lots: int,
                     total_costs: float) -> dict:
    """Compute comprehensive performance metrics."""
    total_trades = len(df)
    winners = df[df["net_pnl"] > 0]
    losers = df[df["net_pnl"] <= 0]

    win_rate = len(winners) / total_trades * 100
    total_net_pnl = df["net_pnl"].sum()
    total_gross_pnl = df["gross_pnl"].sum()

    avg_win = winners["net_pnl"].mean() if len(winners) > 0 else 0
    avg_loss = losers["net_pnl"].mean() if len(losers) > 0 else 0

    # Profit factor
    gross_profit = winners["net_pnl"].sum() if len(winners) > 0 else 0
    gross_loss = abs(losers["net_pnl"].sum()) if len(losers) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown
    cum_pnl = df["net_pnl"].cumsum()
    running_max = cum_pnl.cummax()
    drawdown = cum_pnl - running_max
    max_dd = drawdown.min()
    max_dd_idx = drawdown.idxmin()

    # CAGR
    first_date = pd.to_datetime(df["entry_date"].iloc[0])
    last_date = pd.to_datetime(df["exit_date"].iloc[-1])
    years = (last_date - first_date).days / 365.25

    # Assume starting capital (margin for 1 lot short strangle ≈ ₹1,50,000)
    # For Iron Condor with defined risk, margin ≈ ₹1,00,000 per lot
    starting_capital = 150000 * num_lots
    ending_capital = starting_capital + total_net_pnl
    cagr = ((ending_capital / starting_capital) ** (1 / max(years, 0.1)) - 1) * 100 if ending_capital > 0 else -100

    # Annualized Sharpe (weekly returns)
    df_copy = df.copy()
    df_copy["entry_date_dt"] = pd.to_datetime(df_copy["entry_date"])
    weekly_returns = df_copy["net_pnl"] / starting_capital
    sharpe = (weekly_returns.mean() / weekly_returns.std() * np.sqrt(52)) if weekly_returns.std() > 0 else 0

    # Monthly returns
    df_copy["month"] = df_copy["entry_date_dt"].dt.to_period("M")
    monthly_pnl = df_copy.groupby("month")["net_pnl"].sum()

    # Expectancy per trade
    expectancy = total_net_pnl / total_trades

    # SL stats
    ce_sl_count = df["ce_sl_hit"].sum()
    pe_sl_count = df["pe_sl_hit"].sum()
    both_sl = ((df["ce_sl_hit"]) & (df["pe_sl_hit"])).sum()

    # Yearly breakdown
    df_copy["year"] = df_copy["entry_date_dt"].dt.year
    yearly = df_copy.groupby("year").agg(
        trades=("net_pnl", "count"),
        net_pnl=("net_pnl", "sum"),
        avg_pnl=("net_pnl", "mean"),
        win_rate=("net_pnl", lambda x: (x > 0).mean() * 100),
        max_loss=("net_pnl", "min"),
        max_win=("net_pnl", "max"),
    ).round(2)

    log.info(f"\n{'='*70}")
    log.info(f"RESULTS: {strategy_name}")
    log.info(f"{'='*70}")
    log.info(f"Period: {first_date.date()} → {last_date.date()} ({years:.1f} years)")
    log.info(f"Total Trades: {total_trades}")
    log.info(f"Win Rate: {win_rate:.1f}%")
    log.info(f"")
    log.info(f"Gross P&L:     ₹{total_gross_pnl:>12,.2f}")
    log.info(f"Total Costs:   ₹{total_costs:>12,.2f}")
    log.info(f"Net P&L:       ₹{total_net_pnl:>12,.2f}")
    log.info(f"")
    log.info(f"Starting Capital: ₹{starting_capital:>10,}")
    log.info(f"CAGR:          {cagr:.1f}%")
    log.info(f"Sharpe Ratio:  {sharpe:.2f}")
    log.info(f"Profit Factor: {profit_factor:.2f}")
    log.info(f"Expectancy:    ₹{expectancy:,.2f} per trade")
    log.info(f"")
    log.info(f"Avg Win:       ₹{avg_win:>10,.2f}")
    log.info(f"Avg Loss:      ₹{avg_loss:>10,.2f}")
    log.info(f"Max Drawdown:  ₹{max_dd:>10,.2f}")
    log.info(f"")
    log.info(f"CE SL Hit: {ce_sl_count} ({ce_sl_count/total_trades*100:.1f}%)")
    log.info(f"PE SL Hit: {pe_sl_count} ({pe_sl_count/total_trades*100:.1f}%)")
    log.info(f"Both SL:   {both_sl} ({both_sl/total_trades*100:.1f}%)")
    log.info(f"")
    log.info(f"Yearly Breakdown:")
    log.info(f"\n{yearly.to_string()}")
    log.info(f"")
    log.info(f"Cost Breakdown:")
    log.info(f"  Avg cost per trade: ₹{total_costs/total_trades:.2f}")
    log.info(f"  Cost as % of gross: {total_costs/max(abs(total_gross_pnl),1)*100:.1f}%")

    results = {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_gross_pnl": round(total_gross_pnl, 2),
        "total_costs": round(total_costs, 2),
        "total_net_pnl": round(total_net_pnl, 2),
        "cagr": round(cagr, 2),
        "sharpe": round(sharpe, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "max_drawdown": round(max_dd, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "starting_capital": starting_capital,
        "years": round(years, 2),
        "ce_sl_pct": round(ce_sl_count / total_trades * 100, 1),
        "pe_sl_pct": round(pe_sl_count / total_trades * 100, 1),
        "yearly": yearly.to_dict(),
    }

    return results


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Options Strategy Backtester")
    parser.add_argument("--strategy", default="all",
                        choices=["strangle", "ic", "vix_strangle", "fii_strangle", "all"],
                        help="Strategy to backtest")
    parser.add_argument("--start", type=int, default=2022, help="Start year")
    parser.add_argument("--end", type=int, default=2026, help="End year")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots")
    args = parser.parse_args()

    all_results = {}

    strategies = {
        "strangle": lambda: run_short_strangle_backtest(
            ce_offset=5, pe_offset=-5, sl_multiplier=1.5,
            num_lots=args.lots, start_year=args.start, end_year=args.end,
            strategy_name="S1: Short Strangle (ATM±5, 1.5x SL)",
        ),
        "ic": lambda: run_short_strangle_backtest(
            ce_offset=4, pe_offset=-4, sl_multiplier=2.0,
            num_lots=args.lots, start_year=args.start, end_year=args.end,
            strategy_name="S2: Iron Condor (ATM±4, wings ±3, 2x SL)",
            wing_offset=3,
        ),
        "vix_strangle": lambda: run_short_strangle_backtest(
            ce_offset=5, pe_offset=-5, sl_multiplier=1.5,
            num_lots=args.lots, start_year=args.start, end_year=args.end,
            vix_min=13.0, vix_max=22.0,
            strategy_name="S3: VIX-Filtered Strangle (VIX 13-22, ATM±5, 1.5x SL)",
        ),
        "fii_strangle": lambda: run_short_strangle_backtest(
            ce_offset=5, pe_offset=-5, sl_multiplier=1.5,
            num_lots=args.lots, start_year=args.start, end_year=args.end,
            vix_min=13.0, vix_max=22.0,
            fii_filter=True,
            strategy_name="S4: VIX+FII Filtered Strangle (VIX 13-22, FII filter, ATM±5, 1.5x SL)",
        ),
    }

    if args.strategy == "all":
        to_run = strategies
    else:
        to_run = {args.strategy: strategies[args.strategy]}

    for name, fn in to_run.items():
        result = fn()
        if result:
            all_results[name] = result
            # Save individual trade log
            if "trades_df" in result:
                trades_path = REPORTS_DIR / f"{name}_trades.csv"
                result["trades_df"].to_csv(trades_path, index=False)

    # ─── Comparison Table ────────────────────────────────────────────────
    if len(all_results) > 1:
        log.info(f"\n{'='*90}")
        log.info("STRATEGY COMPARISON")
        log.info(f"{'='*90}")
        comparison = []
        for name, r in all_results.items():
            comparison.append({
                "Strategy": r.get("strategy_name", name),
                "Trades": r["total_trades"],
                "Win%": r["win_rate"],
                "Net P&L": f"₹{r['total_net_pnl']:,.0f}",
                "CAGR": f"{r['cagr']:.1f}%",
                "Sharpe": r["sharpe"],
                "PF": r["profit_factor"],
                "MaxDD": f"₹{r['max_drawdown']:,.0f}",
                "Costs": f"₹{r['total_costs']:,.0f}",
                "Exp/Trade": f"₹{r['expectancy']:,.0f}",
            })
        comp_df = pd.DataFrame(comparison)
        log.info(f"\n{comp_df.to_string(index=False)}")

    # Save summary
    summary = {k: {kk: vv for kk, vv in v.items() if kk != "trades_df"}
                for k, v in all_results.items()}
    summary_path = REPORTS_DIR / "strategy_comparison.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info(f"\nResults saved to {REPORTS_DIR}")


if __name__ == "__main__":
    main()
