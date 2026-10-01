# market-predictor

Backtests of NIFTY weekly options strategies on 1-minute DhanHQ data (2021–2026) and NSE FII/DII participant data. Every result is net of brokerage, STT, exchange fees, GST, slippage and monthly running costs.

| Strategy | Period | CAGR | Max DD | Details |
|---|---|---|---|---|
| 1. Weekly expected-move strangle | 2021–2026 | 12.6% | −32.9% | `final_strategy.py`, `BACKOFF.md` |
| 2. Confirmed directional credit spread | 2022–2026 | 12.4% | −19.1% | [docs/STRATEGY_2_CONFIRMED_SPREAD.md](docs/STRATEGY_2_CONFIRMED_SPREAD.md) |

## Quick start
```bash
pip install -r requirements.txt
python nse_data_downloader.py --start 2022-01-01 --features   # only if participant_oi_features.csv is missing
python build_research_data.py
python -c "import engine; engine.load_days(rebuild=True)"
python final_strategy.py
python final_spread_strategy.py
```
The raw Dhan 1-minute parquet files are expected at `C:\Users\Admin\Documents\Trading Bot\minute_parquet\`.

## Docs
- [docs/HANDOFF.md](docs/HANDOFF.md): where the work stopped and what to do next.
- [docs/INSTITUTIONAL_STRATEGIES.md](docs/INSTITUTIONAL_STRATEGIES.md): delta-hedged, staggered, laddered and gamma-scalping results.
- [report/nifty_strategy_report.html](report/nifty_strategy_report.html): full visual report. Open it in a browser; rebuild with `python report/build_report.py`.
- [CLAUDE.md](CLAUDE.md): short project map for AI agents and new contributors.
- [BACKOFF.md](BACKOFF.md): research log. What was tested, what was rejected, and why.
- [docs/STRATEGY_2_CONFIRMED_SPREAD.md](docs/STRATEGY_2_CONFIRMED_SPREAD.md): full Strategy 2 spec and rebuild steps.
- [strategies/confirmed_spread.json](strategies/confirmed_spread.json): Strategy 2 rules in machine-readable form.

These are backtests, not guarantees. Paper trade before using real money.
