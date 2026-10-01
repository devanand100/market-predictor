# Legacy (invalid) backtests — do not use

These scripts tracked options by rolling `strike_offset` (ATM-relative) across a whole week,
which silently switches contracts every time Nifty moves 50 points. Their reported results
(e.g. "Iron Condor 20.6% CAGR, Sharpe 2.78") are NOT valid. They also used an options
exchange fee ~12x too low. Superseded by engine.py + final_strategy.py (contract-exact).

`backtest_flow_signals.py` (daily FII/DII signal study) is superseded, not broken: its correlations were
in-sample only and flipped sign in 2024-2026 (see research_signals.py). `legacy/reports/` holds the
outputs of these old scripts.
