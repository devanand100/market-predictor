# market-predictor: agent guide

Research and backtesting of NIFTY weekly options strategies for a retail trader, net of real costs. Read this file first. To continue unfinished work, read `docs/HANDOFF.md` next (current status and prioritised next steps). Only open other files when the task needs them.

## Status (2026-10-01)
- **Strategy 1, weekly strangle:** 12.6% CAGR 2021–26, −32.9% max drawdown. Code: `final_strategy.py`. Trades only full 5-session weeks (enters 4 sessions before expiry), so holiday weeks such as the Apr-2025 crash are skipped.
- **Strategy 2, confirmed credit spread:** 6.5% CAGR Jun 2021–Sep 2026, −18.6% max drawdown (2026-10-20, after participant OI was extended back to 2021: 2021 lost 8.4%; from May 2022 it is still 12.2%). Figures below predate that extension. Bear calls carry it; bull puts barely break even. Recommended v2: skip credit < 23.2% of max loss, bull puts at half size → 11.4% CAGR, −8.4% DD. VIX and squeeze filters were rejected (`docs/STRATEGY_2_TRADE_ANALYSIS.md`). Full spec: `docs/STRATEGY_2_CONFIRMED_SPREAD.md`; exported rules: `strategies/confirmed_spread.json`.
- **Institutional structures** (`docs/INSTITUTIONAL_STRATEGIES.md`): delta-hedged ±1× strangle (futures, 1-lot band, 10 lots) = 26.3% CAGR / −14.1% DD (12.3% under pessimistic costs); time-staggered strangle = 20.2% / −20.1%. Ladders, leg-rolling and gamma scalping lose.
- **Strategy 3, weekly gap-direction option buy** (`final_gap_buy.py`, `docs/OPTION_BUYING.md`): ATM CE/PE in the opening-gap direction, 4 DTE, +100% target, no stop, VIX ≤ 15, 5% of equity → 16.6% CAGR / −13.8% DD from ₹3L (manual, no running costs). All other buying ideas (ORB, 0DTE, long straddles, VIX > 15) fail OOS. ₹3L plan: 50/50 with Strategy 2 v2 = 13.2% CAGR / −9.3% DD (Jun 2021+; 100% gap buy 17.9% / −13.8%, `research_buy_spread_combo.py`); checklist `docs/PAPER_TRADING_3L.md`.
- **Strategy 4, stacked strangle + low-VIX gap buy** (`final_stacked_strategy.py`, `docs/STACKED_STRATEGY.md`): Strategy 1 at 30% margin + Strategy 3 at 3% on VIX ≤ 15 weeks, same account → 20.2% CAGR / −17.2% DD at ₹10L with running costs (2021–23 10.5%, 2024–26 31.7%). Needs ~₹10L; at ₹3L 14.2% / −30.5%.
- **Strategy 5, three-sleeve portfolio** (`final_portfolio_strategy.py`, `docs/PORTFOLIO_STRATEGY.md`): L2 staggered strangle 30% margin + gap buy 3% (VIX ≤ 15) + monthly straddle 20% margin (VRP ≥ 2) → 27.3% CAGR / −16.9% DD at ₹10L with running costs, every year positive (2021–23 18.4%, 2024–26 37.8%). At Nifty ~25k the L2 sleeve needs ~₹15L; does not work at ₹3L.
- **Strategy 6, VIX-regime portfolio on a real-market replay** (`final_realtrade_strategy.py`, `docs/STRATEGY_6_REALTRADE.md`, 2026-10-21): previous-session VIX ≥ 15 → short strangle ±1.25× straddle 2 sessions before expiry (stop 1×) + expiry-day strangle ±0.5× (stop 0.5×), 45% margin each; VIX ≤ 13 → gap buy 5% → 38.7% CAGR / −13.5% DD / trade PF 1.76 at ₹10L with running costs (OOS 2024+ 27.5% / −13.4% / 1.58; every year positive). Simulator `research_realtrade.py` fills only at real traded prices, NSE lot size by date, margin cap. **It shows the old engine's intrinsic pricing inflated short strangles: L2 drops from PF 1.40 to 1.03 on real prices, so treat Strategy 5's numbers as optimistic.**
- **Strategy 5 on real prices** (`docs/STRATEGY_5_REAL_CHECK.md`, 2026-10-21): at ₹15L it gives 19.3% CAGR / −20.7% DD / PF 1.41, not 29.4%. About 7.5 CAGR points were synthetic edge: L2 lost about 33% of its points and the monthly straddle up to 48%; the gap buy had none.
- **Paper bot for Strategy 5 (removed from AWS 2026-10-22, replaced by `strategy6_bot/`):** `three_sleeve_bot/` (Lambda + EventBridge Scheduler + Dhan data + MongoDB journal + one Discord message per run). Read `three_sleeve_bot/README.md`. Deployed 2026-10-21; scheduled runs from 2026-10-22.
- **Monthly 45→21 DTE short straddle** (`docs/MONTHLY_OPTIONS.md`, `research_monthly_*.py`): VRP ≥ 2 filter → 20.5% CAGR / −23.1% DD; stops, re-centring and condors rejected.
- Results and what was rejected: `BACKOFF.md`, section 2 (e.g. 0DTE straddles, FII futures signals, PCR, trend).
- Report: `report/nifty_strategy_report.html` (open in a browser), rebuilt by `python report/build_report.py`. Published copy: https://claude.ai/artifact/9zrJVS9fsbgyGPQuMTkk8r

## Data traps (mistakes already made once)
1. The Dhan options `strike_offset` is **rolling ATM-relative**. Key every leg by the absolute `strike` via `engine.Day.px()`. Backtests in `legacy/` broke this rule and are invalid.
2. The options data covers the nearest weekly expiry only, ±10 strikes (±500 points) around ATM. Legs outside that window use intrinsic value. Never forward-fill across the window edge (bug fixed 2026-10-01; it had overstated Strategy 2 by about 6 CAGR points).
3. Expiries are detected from the data (`daily_features.is_expiry`). Never hard-code Thursday.
4. NSE participant OI is used lagged one session (`prev_*` columns). Same-day values are look-ahead bias.
5. Build the engine cache with `python -c "import engine; engine.load_days(rebuild=True)"`, not `python engine.py`, which breaks the pickle.
6. 2025-10-21 (Muhurat) has no 09:30 option prices; long legs get the 0.05 intrinsic fallback. Skip days with no prices at the entry minute (`run_cycle` guards only shorts).
7. Dhan spot contains NSE Saturday **mock** sessions (8 in 2021–22, fake moves up to +4.7%). `build_research_data.build_daily` drops weekend rows without options data; real special sessions (budget days, DR drills) stay.
8. Choose parameters on 2021–2023 only and treat 2024+ as out-of-sample. Compare any direction signal against always-bull, always-bear and random-direction baselines.

## File map
| File | Role |
|---|---|
| `nse_data_downloader.py` | Scrapes NSE participant OI (2021-01 onward, incl. weekend special sessions) → `data/processed/participant_oi_features.csv` |
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
| `research_option_buying.py`, `research_option_buying_positional.py` | Option-buying grids (intraday ORB; long straddles and directional weekly buys) |
| `final_gap_buy.py` | Strategy 3 sized backtest from ₹3L, Monte Carlo |
| `research_regime_switch.py`, `final_stacked_strategy.py` | VIX switch / stacking of Strategies 1 and 3; Strategy 4 final backtest |
| `strategy6_bot/` | Strategy 6 paper bot, ₹5L paper capital (₹3L at first deploy, then ₹4L), deployed 2026-10-22; checks Dhan's real margin before each strangle entry (Lambda `Strategy6PaperBot`, Mongo `strategy6`, Discord `#strategy6-paper`); `rules.py` is parity-tested against `research_realtrade.py`. See its README |
| `research_realtrade_dash_export.py`, `research_realtrade_sleeve_export.py` | Strategy 6 at ₹3L with bot settings → `data/reports/strategy6_3L_*.json` for the dashboard https://claude.ai/artifact/FH3SrDnkrnJ81ds3T1jsXR (trades, daily equity, fixed-₹3L capacity, each sleeve alone) |
| `research_realtrade.py` | Real-market replay: account + `Weekly` structures, real prices only, lot size by date, margin cap, `replay_many()` runs many accounts in one pass |
| `research_realtrade_grid*.py`, `research_realtrade_portfolio.py`, `research_realtrade_sensitivity.py`, `final_realtrade_strategy.py` | Strategy 6: structure grids, sizing grid, VIX-threshold sensitivity, final backtest + stress tests |
| `three_sleeve_bot/` | Strategy 5 paper-trading bot (see its README); `rules.py` is parity-tested against the research |
| `research_portfolio_sleeves.py`, `final_portfolio_strategy.py` | Multi-sleeve weight search (S1/L2/D2-pessimistic/gap/monthly/bear calls); Strategy 5 event-driven lot-exact backtest |
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
python final_realtrade_strategy.py base               # Strategy 6 (~1 min); must give CAGR 38.7, DD -13.5, PF 1.76 (no arg = 9 stress cases, ~10 min)
python final_spread_strategy.py                        # Strategy 2 (~10 s); must give 144 trades, final equity Rs 6,96,876
python research_trade_analysis.py && python report/build_report.py   # refresh the report
```

## Conventions
- Research P&L is per unit and then multiplied by `rp.QTY` (75 in research, 65 in final sizing). Statutory costs are per order at that quantity.
- Sell fills at `close − slip`, buy fills at `close + slip`. A stop triggers on the 1-minute high and fills at `max(stop, close)` plus urgent slippage.
- New research goes in a new `research_*.py` script that writes a CSV to `data/reports/`. Only add a `final_*.py` for a strategy that passes the out-of-sample test.
