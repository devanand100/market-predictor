"""
Institutional Quant Strategy: Multi-Regime Adaptive Iron Condor & Skewed Spreads
combining DhanHQ 1-minute options data, India VIX regime, and NSE FII/DII flow momentum.

Developed with prop-desk methodology:
1. Dynamic Strike Skewing based on FII Flow Momentum
2. VIX Regime Filter & Volatility Gating
3. 50% Take Profit (TP) Rule (harvest theta early, avoid gamma risk on expiry day)
4. Defined-Risk Wing Hedging for SEBI margin relief (retail capital friendly: ~₹80k-₹1.2L margin)
5. Full SEBI 2024-2026 Statutory Cost Accounting + 0.5% Slippage on every fill
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from cost_model import (
    get_lot_size,
    compute_round_trip_costs,
    compute_trade_costs,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PARQUET_BASE = Path(r"C:\Users\Admin\Documents\Trading Bot\minute_parquet")
FII_DATA_PATH = Path(r"C:\Users\Admin\Documents\market-predictor\data\processed\participant_oi_features.csv")
REPORTS_DIR = Path(__file__).parent / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

SLIPPAGE_PCT = 0.005  # 0.5% adverse slippage on every fill


def load_all_market_data(start_year: int = 2022):
    """Load spot daily, vix daily, and FII daily flow features."""
    log.info("Loading Spot and VIX minute data for daily profiles...")
    spot = pd.read_parquet(PARQUET_BASE / "spot")
    spot["timestamp"] = pd.to_datetime(spot["timestamp"])
    spot = spot[spot["timestamp"].dt.year >= start_year].copy()
    spot = spot.set_index("timestamp").sort_index()

    spot_daily = spot.resample("D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

    vix = pd.read_parquet(PARQUET_BASE / "vix")
    vix["timestamp"] = pd.to_datetime(vix["timestamp"])
    vix = vix[vix["timestamp"].dt.year >= start_year].copy()
    vix = vix.set_index("timestamp").sort_index()

    vix_daily = vix.resample("D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last"
    }).dropna()
    vix_daily = vix_daily.rename(columns={"open": "vix_open", "high": "vix_high",
                                           "low": "vix_low", "close": "vix_close"})

    fii_daily = None
    if FII_DATA_PATH.exists():
        fii_daily = pd.read_csv(FII_DATA_PATH, parse_dates=["date"])
        fii_daily["date"] = pd.to_datetime(fii_daily["date"]).dt.normalize()
        fii_daily = fii_daily.set_index("date").sort_index()
        log.info(f"FII Data indexed: {len(fii_daily)} days")

    return spot_daily, vix_daily, fii_daily


def build_expiry_calendar(spot_daily: pd.DataFrame) -> list:
    """Build exact NSE weekly expiry calendar."""
    trading_days = sorted(spot_daily.index.normalize().unique())
    td_set = set(trading_days)
    expiries = []
    for td in trading_days:
        dow = td.dayofweek
        if dow == 3 and td in td_set:  # Thursday
            expiries.append(td)
        elif dow == 2:  # Wednesday (if Thursday is holiday)
            next_thu = td + pd.Timedelta(days=1)
            if next_thu not in td_set:
                expiries.append(td)
    return expiries


def get_options_chunk(year: int, month: int, cache: dict) -> Optional[pd.DataFrame]:
    """Retrieve monthly options parquet from cache or disk."""
    key = (year, month)
    if key in cache:
        return cache[key]
    path = PARQUET_BASE / "options" / f"year={year}" / f"month={month:02d}" / "options.parquet"
    if not path.exists():
        cache[key] = None
        return None
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    cache[key] = df
    # Keep cache small (max 4 months)
    if len(cache) > 4:
        oldest_key = next(iter(cache))
        if oldest_key != key:
            del cache[oldest_key]
    return df


def get_options_for_cycle(entry_date: datetime, exit_date: datetime, cache: dict) -> Optional[pd.DataFrame]:
    """Load combined options dataframe covering cycle entry to exit."""
    df1 = get_options_chunk(entry_date.year, entry_date.month, cache)
    if entry_date.month == exit_date.month and entry_date.year == exit_date.year:
        return df1
    df2 = get_options_chunk(exit_date.year, exit_date.month, cache)
    if df1 is not None and df2 is not None:
        return pd.concat([df1, df2], ignore_index=True)
    return df1 if df1 is not None else df2


def get_price_at(opts: pd.DataFrame, date_str: str, time_str: str, offset: int, opt_type: str) -> Optional[float]:
    """Fetch option close price with adverse slippage."""
    sub = opts[(opts["date"] == date_str) & (opts["time"] == time_str) &
               (opts["strike_offset"] == offset) & (opts["option_type"] == opt_type)]
    if not sub.empty:
        return float(sub.iloc[0]["close"])
    # fallback search up to 15 mins later
    sub = opts[(opts["date"] == date_str) & (opts["time"] >= time_str) &
               (opts["strike_offset"] == offset) & (opts["option_type"] == opt_type)]
    if not sub.empty:
        return float(sub.iloc[0]["close"])
    return None


def run_adaptive_quant_strategy(
    take_profit_pct: float = 0.50,  # 50% Take Profit
    stop_loss_mult: float = 1.75,   # 1.75x SL on short leg
    wing_width: int = 3,            # 3 strikes hedge protection
    use_fii_skew: bool = True,      # Skew strikes based on FII net flow
    vix_min: float = 11.5,          # Avoid trading in dead low-IV regime
    vix_max: float = 26.0,          # Avoid extreme crash/black-swan weeks
    start_year: int = 2022,
    end_year: int = 2026,
    num_lots: int = 1,
) -> dict:
    """
    Executes the institutional quant options strategy across DhanHQ 1-minute data.
    """
    log.info(f"\n{'='*80}")
    log.info("RUNNING INSTITUTIONAL ADAPTIVE QUANT STRATEGY BACKTEST")
    log.info(f"Take Profit: {take_profit_pct*100}% | Stop Loss: {stop_loss_mult}x | Wing Width: {wing_width}")
    log.info(f"FII Skewing: {use_fii_skew} | VIX Gate: [{vix_min}, {vix_max}] | Lots: {num_lots}")
    log.info(f"{'='*80}")

    spot_daily, vix_daily, fii_daily = load_all_market_data(start_year)
    expiries = build_expiry_calendar(spot_daily)
    trading_days_set = set(spot_daily.index.normalize())

    cycles = []
    for i in range(1, len(expiries)):
        entry_d = expiries[i - 1]
        exit_d = expiries[i]
        c_days = sorted([d for d in trading_days_set if entry_d <= d <= exit_d])
        if len(c_days) >= 2 and entry_d.year >= start_year and exit_d.year <= end_year:
            cycles.append({"entry_date": entry_d, "exit_date": exit_d, "days": c_days})

    log.info(f"Total weekly expiry cycles to test: {len(cycles)}")

    opts_cache = {}
    trades = []
    skipped_vix = 0
    skipped_data = 0

    for ci, cycle in enumerate(cycles):
        entry_date = cycle["entry_date"]
        exit_date = cycle["exit_date"]
        entry_str = entry_date.strftime("%Y-%m-%d")
        exit_str = exit_date.strftime("%Y-%m-%d")
        lot_size = get_lot_size(entry_str)

        # 1. VIX Gate
        vix_val = None
        if entry_date in vix_daily.index:
            vix_val = float(vix_daily.loc[entry_date, "vix_close"])
        if vix_val is not None and (vix_val < vix_min or vix_val > vix_max):
            skipped_vix += 1
            continue

        # 2. Determine Strike Offsets based on FII Flow Skew
        # Default symmetric Iron Condor: short ATM+4 CE, short ATM-4 PE
        ce_short_offset = 4
        pe_short_offset = -4

        fii_regime = "NEUTRAL"
        if use_fii_skew and fii_daily is not None and entry_date in fii_daily.index:
            fii_row = fii_daily.loc[entry_date]
            fii_net_chg = fii_row.get("fii_fut_net_change", 0)
            fii_opt_sent = fii_row.get("fii_options_sentiment", 0)

            # Bullish Flow: FII buying futures or heavy PE writing
            if fii_net_chg > 5000 or fii_opt_sent > 0.15:
                ce_short_offset = 5   # Push CE further OTM (allow upside drift)
                pe_short_offset = -3   # Bring PE closer to harvest rich put premium
                fii_regime = "BULLISH_FLOW"
            # Bearish Flow: FII aggressively dumping futures or heavy CE writing
            elif fii_net_chg < -5000 or fii_opt_sent < -0.15:
                ce_short_offset = 3   # Bring CE closer
                pe_short_offset = -5   # Push PE further OTM
                fii_regime = "BEARISH_FLOW"

        ce_wing_offset = ce_short_offset + wing_width
        pe_wing_offset = pe_short_offset - wing_width

        # 3. Load Options Data for Cycle
        opts = get_options_for_cycle(entry_date, exit_date, opts_cache)
        if opts is None:
            skipped_data += 1
            continue

        # 4. Entry Execution at 09:20 on Cycle Entry Day
        entry_time = "09:20"
        ce_short_p = get_price_at(opts, entry_str, entry_time, ce_short_offset, "CE")
        pe_short_p = get_price_at(opts, entry_str, entry_time, pe_short_offset, "PE")
        ce_wing_p = get_price_at(opts, entry_str, entry_time, ce_wing_offset, "CE")
        pe_wing_p = get_price_at(opts, entry_str, entry_time, pe_wing_offset, "PE")

        if not all([ce_short_p, pe_short_p, ce_wing_p, pe_wing_p]):
            skipped_data += 1
            continue

        if ce_short_p < 4.0 or pe_short_p < 4.0:
            skipped_data += 1
            continue

        # Slippage: Sell at (p * 0.995), Buy at (p * 1.005)
        ce_sell_fill = ce_short_p * (1.0 - SLIPPAGE_PCT)
        pe_sell_fill = pe_short_p * (1.0 - SLIPPAGE_PCT)
        ce_wing_fill = ce_wing_p * (1.0 + SLIPPAGE_PCT)
        pe_wing_fill = pe_wing_p * (1.0 + SLIPPAGE_PCT)

        net_credit_entry = (ce_sell_fill + pe_sell_fill) - (ce_wing_fill + pe_wing_fill)
        if net_credit_entry <= 0:
            skipped_data += 1
            continue

        # Targets & Stop Loss Thresholds
        target_profit_prem = net_credit_entry * (1.0 - take_profit_pct)  # cost to close <= this
        ce_sl_threshold = ce_sell_fill * stop_loss_mult
        pe_sl_threshold = pe_sell_fill * stop_loss_mult

        # 5. Minute-by-Minute Lifecycle Management
        ce_closed = False
        pe_closed = False
        ce_exit_fill = 0.0
        pe_exit_fill = 0.0
        ce_wing_exit_fill = 0.0
        pe_wing_exit_fill = 0.0
        exit_reason = "EXPIRY"
        actual_exit_date = exit_str
        actual_exit_time = "15:15"

        for day in cycle["days"]:
            day_str = day.strftime("%Y-%m-%d")
            is_expiry = (day == exit_date)

            day_opts = opts[opts["date"] == day_str]
            if day_opts.empty:
                continue

            # Iterate through 15-minute checkpoints for speed + 1-min near boundaries
            # Check timestamps available for this day
            timestamps = sorted(day_opts["time"].unique())
            start_idx = 0
            if day == entry_date:
                # start after 09:25
                timestamps = [t for t in timestamps if t >= "09:25"]

            for t in timestamps:
                # Fetch current prices
                cur_ce = get_price_at(day_opts, day_str, t, ce_short_offset, "CE")
                cur_pe = get_price_at(day_opts, day_str, t, pe_short_offset, "PE")
                cur_ce_w = get_price_at(day_opts, day_str, t, ce_wing_offset, "CE") or 0.5
                cur_pe_w = get_price_at(day_opts, day_str, t, pe_wing_offset, "PE") or 0.5

                if cur_ce is None or cur_pe is None:
                    continue

                # A. Take Profit Check (Whole Condor Value Decayed >= 50%)
                current_cost_to_close = (cur_ce + cur_pe) - (cur_ce_w + cur_pe_w)
                if current_cost_to_close <= target_profit_prem:
                    ce_exit_fill = cur_ce * (1.0 + SLIPPAGE_PCT)
                    pe_exit_fill = cur_pe * (1.0 + SLIPPAGE_PCT)
                    ce_wing_exit_fill = cur_ce_w * (1.0 - SLIPPAGE_PCT)
                    pe_wing_exit_fill = cur_pe_w * (1.0 - SLIPPAGE_PCT)
                    ce_closed = True
                    pe_closed = True
                    exit_reason = f"TAKE_PROFIT_50% ({day_str} {t})"
                    actual_exit_date = day_str
                    actual_exit_time = t
                    break

                # B. Stop Loss Check on CE Leg
                if not ce_closed and cur_ce >= ce_sl_threshold:
                    ce_exit_fill = cur_ce * (1.0 + SLIPPAGE_PCT)
                    ce_wing_exit_fill = cur_ce_w * (1.0 - SLIPPAGE_PCT)
                    ce_closed = True
                    exit_reason = f"CE_SL_HIT ({day_str} {t})"
                    actual_exit_date = day_str
                    actual_exit_time = t

                # C. Stop Loss Check on PE Leg
                if not pe_closed and cur_pe >= pe_sl_threshold:
                    pe_exit_fill = cur_pe * (1.0 + SLIPPAGE_PCT)
                    pe_wing_exit_fill = cur_pe_w * (1.0 - SLIPPAGE_PCT)
                    pe_closed = True
                    exit_reason = f"PE_SL_HIT ({day_str} {t})" if not ce_closed else "BOTH_SL_HIT"
                    actual_exit_date = day_str
                    actual_exit_time = t

                # If both short legs closed, exit wings as well
                if ce_closed and pe_closed:
                    break

            if ce_closed and pe_closed:
                break

            # D. Expiry Day Final Settlement at 15:15
            if is_expiry and not (ce_closed and pe_closed):
                exp_t = "15:15"
                if not ce_closed:
                    p = get_price_at(day_opts, day_str, exp_t, ce_short_offset, "CE") or 0.25
                    ce_exit_fill = p * (1.0 + SLIPPAGE_PCT)
                    w = get_price_at(day_opts, day_str, exp_t, ce_wing_offset, "CE") or 0.10
                    ce_wing_exit_fill = w * (1.0 - SLIPPAGE_PCT)
                    ce_closed = True

                if not pe_closed:
                    p = get_price_at(day_opts, day_str, exp_t, pe_short_offset, "PE") or 0.25
                    pe_exit_fill = p * (1.0 + SLIPPAGE_PCT)
                    w = get_price_at(day_opts, day_str, exp_t, pe_wing_offset, "PE") or 0.10
                    pe_wing_exit_fill = w * (1.0 - SLIPPAGE_PCT)
                    pe_closed = True

                exit_reason = "EXPIRED_AT_CLOSE"
                actual_exit_date = day_str
                actual_exit_time = exp_t
                break

        # 6. Calculate P&L and Deduct Statutory Friction
        gross_short_ce_pnl = (ce_sell_fill - ce_exit_fill) * lot_size * num_lots
        gross_short_pe_pnl = (pe_sell_fill - pe_exit_fill) * lot_size * num_lots
        gross_wing_ce_pnl = (ce_wing_exit_fill - ce_wing_fill) * lot_size * num_lots
        gross_wing_pe_pnl = (pe_wing_exit_fill - pe_wing_fill) * lot_size * num_lots

        gross_trade_pnl = gross_short_ce_pnl + gross_short_pe_pnl + gross_wing_ce_pnl + gross_wing_pe_pnl

        # Statutory Costs for all 4 legs (entry + exit)
        cost_ce_short = compute_round_trip_costs(ce_sell_fill, ce_exit_fill, lot_size, num_lots, entry_str, is_short=True)
        cost_pe_short = compute_round_trip_costs(pe_sell_fill, pe_exit_fill, lot_size, num_lots, entry_str, is_short=True)
        cost_ce_wing = compute_round_trip_costs(ce_wing_fill, ce_wing_exit_fill, lot_size, num_lots, entry_str, is_short=False)
        cost_pe_wing = compute_round_trip_costs(pe_wing_fill, pe_wing_exit_fill, lot_size, num_lots, entry_str, is_short=False)

        trade_costs = (cost_ce_short["total_round_trip"] + cost_pe_short["total_round_trip"] +
                       cost_ce_wing["total_round_trip"] + cost_pe_wing["total_round_trip"])

        net_trade_pnl = gross_trade_pnl - trade_costs

        trades.append({
            "entry_date": entry_str,
            "exit_date": actual_exit_date,
            "exit_time": actual_exit_time,
            "exit_reason": exit_reason,
            "fii_regime": fii_regime,
            "vix": round(vix_val, 2) if vix_val else None,
            "lot_size": lot_size,
            "ce_short_strike": ce_short_offset,
            "pe_short_strike": pe_short_offset,
            "net_credit_entry": round(net_credit_entry, 2),
            "gross_pnl": round(gross_trade_pnl, 2),
            "total_costs": round(trade_costs, 2),
            "net_pnl": round(net_trade_pnl, 2),
            "is_win": net_trade_pnl > 0,
        })

    log.info(f"Backtest completed: {len(trades)} executed trades, {skipped_vix} VIX skips, {skipped_data} data skips")

    if not trades:
        return {}

    df = pd.DataFrame(trades)
    df["cum_net_pnl"] = df["net_pnl"].cumsum()

    # Performance Metrics
    total_trades = len(df)
    winners = df[df["is_win"]]
    losers = df[~df["is_win"]]
    win_rate = len(winners) / total_trades * 100

    total_gross = df["gross_pnl"].sum()
    total_costs = df["total_costs"].sum()
    total_net = df["net_pnl"].sum()

    gross_profit = winners["net_pnl"].sum()
    gross_loss = abs(losers["net_pnl"].sum()) if len(losers) > 0 else 1.0
    profit_factor = gross_profit / gross_loss

    # Max Drawdown
    cum = df["net_pnl"].cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd = dd.min()

    # Capital & CAGR (Margin with wings for 1 lot Nifty Condor is ~₹85,000 - ₹1,00,000)
    starting_capital = 100000.0 * num_lots
    years = (pd.to_datetime(df["exit_date"].iloc[-1]) - pd.to_datetime(df["entry_date"].iloc[0])).days / 365.25
    cagr = (( (starting_capital + total_net) / starting_capital ) ** (1.0 / max(years, 0.1)) - 1.0) * 100.0

    # Sharpe & Expectancy
    weekly_ret = df["net_pnl"] / starting_capital
    sharpe = (weekly_ret.mean() / weekly_ret.std() * np.sqrt(52)) if weekly_ret.std() > 0 else 0
    sortino = (weekly_ret.mean() / weekly_ret[weekly_ret < 0].std() * np.sqrt(52)) if len(weekly_ret[weekly_ret < 0]) > 0 else 0
    expectancy = total_net / total_trades

    # Yearly Table
    df["year"] = pd.to_datetime(df["entry_date"]).dt.year
    yearly = df.groupby("year").agg(
        trades=("net_pnl", "count"),
        net_pnl=("net_pnl", "sum"),
        win_rate=("is_win", lambda x: round(x.mean() * 100, 1)),
        avg_trade=("net_pnl", lambda x: round(x.mean(), 2)),
        max_loss=("net_pnl", "min"),
        max_win=("net_pnl", "max"),
    )

    # Exit reason breakdown
    reason_counts = df["exit_reason"].apply(lambda x: x.split(" ")[0]).value_counts()

    return {
        "trades_df": df,
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "total_gross_pnl": round(total_gross, 2),
        "total_costs": round(total_costs, 2),
        "total_net_pnl": round(total_net, 2),
        "starting_capital": starting_capital,
        "cagr": round(cagr, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_dd, 2),
        "expectancy_per_trade": round(expectancy, 2),
        "avg_win": round(winners["net_pnl"].mean(), 2) if len(winners) > 0 else 0,
        "avg_loss": round(losers["net_pnl"].mean(), 2) if len(losers) > 0 else 0,
        "years": round(years, 2),
        "cost_pct_of_gross": round(total_costs / max(abs(total_gross), 1) * 100, 1),
        "yearly_summary": yearly.to_dict(orient="index"),
        "exit_reasons": reason_counts.to_dict(),
    }


def main():
    parser = argparse.ArgumentParser(description="Adaptive Quant Iron Condor Backtester")
    parser.add_argument("--tp", type=float, default=0.50, help="Take Profit target (default: 0.50 = 50%%)")
    parser.add_argument("--sl", type=float, default=1.75, help="Stop loss multiplier (default: 1.75x)")
    parser.add_argument("--wings", type=int, default=3, help="Wing width strikes (default: 3)")
    parser.add_argument("--fii_skew", action="store_true", default=True, help="Enable FII flow skewing")
    parser.add_argument("--no_fii_skew", dest="fii_skew", action="store_false")
    args = parser.parse_args()

    results = run_adaptive_quant_strategy(
        take_profit_pct=args.tp,
        stop_loss_mult=args.sl,
        wing_width=args.wings,
        use_fii_skew=args.fii_skew,
    )

    if not results:
        log.error("Backtest produced no output.")
        return

    # Print Report
    log.info("\n" + "="*80)
    log.info("FINAL QUANTITATIVE STRATEGY TEARSHEET (PROP DESK AUDIT)")
    log.info("="*80)
    log.info(f"Trading Horizon:      {results['years']} Years (2022 - 2026)")
    log.info(f"Total Cycles Traded:  {results['total_trades']}")
    log.info(f"Win Rate:             {results['win_rate']}%")
    log.info(f"Profit Factor:        {results['profit_factor']}")
    log.info(f"Sharpe Ratio:         {results['sharpe']}")
    log.info(f"Sortino Ratio:        {results['sortino']}")
    log.info(f"CAGR (Return on Cap): {results['cagr']}%")
    log.info(f"Net Realized Profit:  ₹{results['total_net_pnl']:,.2f}")
    log.info(f"Total Running Costs:  ₹{results['total_costs']:,.2f} ({results['cost_pct_of_gross']}% of gross)")
    log.info(f"Max Capital Drawdown: ₹{results['max_drawdown']:,.2f}")
    log.info(f"Expectancy / Trade:   ₹{results['expectancy_per_trade']:,.2f}")
    log.info(f"Average Winner:       ₹{results['avg_win']:,.2f}")
    log.info(f"Average Loser:        ₹{results['avg_loss']:,.2f}")
    log.info(f"\nExit Breakdown:       {results['exit_reasons']}")
    log.info("\nYearly Consistency Breakdown:")
    for yr, d in results["yearly_summary"].items():
        log.info(f"  {yr}: Net ₹{d['net_pnl']:>10,.2f} | Win {d['win_rate']:>5.1f}% | Avg Trade ₹{d['avg_trade']:>8,.2f} | Trades: {d['trades']}")

    # Save to disk
    report_file = REPORTS_DIR / "adaptive_quant_strategy_summary.json"
    with open(report_file, "w") as f:
        export_data = {k: v for k, v in results.items() if k != "trades_df"}
        json.dump(export_data, f, indent=2)

    trades_file = REPORTS_DIR / "adaptive_quant_strategy_trades.csv"
    results["trades_df"].to_csv(trades_file, index=False)
    log.info(f"\nFull trade ledger saved to: {trades_file}")
    log.info(f"Executive JSON summary saved to: {report_file}")


if __name__ == "__main__":
    main()
