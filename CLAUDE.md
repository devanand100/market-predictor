# market-predictor: agent guide

Research and backtesting of NIFTY weekly options strategies for a retail trader, net of real costs. Read this file first. To continue unfinished work, read `docs/HANDOFF.md` next (current status and prioritised next steps). Only open other files when the task needs them.

## Status (2026-10-01)
- **Strategy 1, weekly strangle:** 12.6% CAGR 2021–26, −32.9% max drawdown. Code: `final_strategy.py`. Trades only full 5-session weeks (enters 4 sessions before expiry), so holiday weeks such as the Apr-2025 crash are skipped.
- **Strategy 2, confirmed credit spread:** 12.4% CAGR 2022–26, −19.1% max drawdown. Bear calls carry it; bull puts barely break even. Recommended v2: skip credit < 23.2% of max loss, bull puts at half size → 11.4% CAGR, −8.4% DD. VIX and squeeze filters were rejected (`docs/STRATEGY_2_TRADE_ANALYSIS.md`). Full spec: `docs/STRATEGY_2_CONFIRMED_SPREAD.md`; exported rules: `strategies/confirmed_spread.json`.
- **Institutional structures** (`docs/INSTITUTIONAL_STRATEGIES.md`): delta-hedged ±1× strangle (futures, 1-lot band, 10 lots) = 26.3% CAGR / −14.1% DD (12.3% under pessimistic costs); time-staggered strangle = 20.2% / −20.1%. Ladders, leg-rolling and gamma scalping lose.
- Results and what was rejected: `BACKOFF.md`, section 2 (e.g. 0DTE straddles, FII futures signals, PCR, trend).
- Report: `report/nifty_strategy_report.html` (open in a browser), rebuilt by `python report/build_report.py`. Published copy: https://claude.ai/artifact/9zrJVS9fsbgyGPQuMTkk8r

## Data traps (mistakes already made once)
1. The Dhan options `strike_offset` is **rolling ATM-relative**. Key every leg by the absolute `strike` via `engine.Day.px()`. Backtests in `legacy/` broke this rule and are invalid.
2. The options data covers the nearest weekly expiry only, ±10 strikes (±500 points) around ATM. Legs outside that window use intrinsic value. Never forward-fill across the window edge (bug fixed 2026-10-01; it had overstated Strategy 2 by about 6 CAGR points).
3. Expiries are detected from the data (`daily_features.is_expiry`). Never hard-code Thursday.
4. NSE participant OI is used lagged one session (`prev_*` columns). Same-day values are look-ahead bias.
5. Build the engine cache with `python -c "import engine; engine.load_days(rebuild=True)"`, not `python engine.py`, which breaks the pickle.
6. Choose parameters on 2021–2023 only and treat 2024+ as out-of-sample. Compare any direction signal against always-bull, always-bear and random-direction baselines.

## File map
| File | Role |
|---|---|
| `nse_data_downloader.py` | Scrapes NSE participant OI → `data/processed/participant_oi_features.csv` |
| `update_market_data.py` | Incremental Dhan download of spot, VIX and options into `data/raw/minute_parquet/`, then rebuilds research data and the engine cache |
| `build_research_data.py` | Dhan parquet → `options_1min.parquet` (wide, by absolute strike) + `daily_features.parquet` (spot, VIX, expiries, lagged FII) |
| `engine.py` | Per-day [minute × strike] price matrices. `load_days()`, `load_daily()`, `Day.px/series`. Minute 0 = 09:15, 360 = 15:15 |
| `cost_model.py` | Date-aware statutory charges, `slip()`, `MONTHLY_RUNNING_COSTS` |
| `research_positional.py` | `cycles()` (weekly windows), `run_cycle()` (multi-day, contract-exact, MTM stop/target). Core simulator |
| `research_directional_spreads.py` | `signal_table()` (15 weekly signals), `make_legs_fn()` (credit/debit verticals) |
| `research_spread_validation.py` | Permutation tests of the signals |
| `research_intraday.py`, `research_strangle.py`, `research_signals.py` | Earlier research grids |
| `final_strategy.py` | Strategy 1 sized backtest, Monte Carlo, `metrics()` |
| `final_spread_strategy.py` | Strategy 2 sized backtest + 50/50 portfolio with Strategy 1 |
| `research_trade_analysis.py` | Per-trade context for Strategy 2 → `spread_trades_enriched.csv` |
| `research_institutional.py`, `research_institutional_stress.py` | Ladders, staggered entry, delta-hedged (Black-Scholes + futures), leg-rolling, gamma scalping; `Book` class handles options + futures with costs |
| `research_vix_regime.py`, `research_vix_filters_bt.py` | VIX, reward-to-risk and squeeze (compression → expansion) filters |
| `report/` | `build_report.py` + `report_template.html` + `section_*.html` → `nifty_strategy_report.html`. Charts use the JSON outputs; tables in `section_*.html` are hand-written |
| `data/reports/` | Outputs (`*_summary.json`, `*_trades.csv`) |
| `legacy/` | Invalid or superseded code. Do not use |

Raw data: `data/raw/minute_parquet/` (git-ignored; options, spot, VIX; 1-min; Jan 2021 onward). Refresh with `update_market_data.py`, which needs `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in `.env`.

## Commands
```bash
python update_market_data.py                           # fetch new Dhan data + rebuild everything below it
python build_research_data.py --rebuild                # after new Dhan data (~1.5 min)
python -c "import engine; engine.load_days(rebuild=True)"   # after rebuilding data (~20 s)
python final_strategy.py                               # Strategy 1 (~1 min)
python final_spread_strategy.py                        # Strategy 2 (~10 s); must give 119 trades, final equity Rs 8,24,583
python research_trade_analysis.py && python report/build_report.py   # refresh the report
```

## Conventions
- Research P&L is per unit and then multiplied by `rp.QTY` (75 in research, 65 in final sizing). Statutory costs are per order at that quantity.
- Sell fills at `close − slip`, buy fills at `close + slip`. A stop triggers on the 1-minute high and fills at `max(stop, close)` plus urgent slippage.
- New research goes in a new `research_*.py` script that writes a CSV to `data/reports/`. Only add a `final_*.py` for a strategy that passes the out-of-sample test.
