# Handoff: resume here (last updated 2026-10-01)

Read [../CLAUDE.md](../CLAUDE.md) first (project map, data traps, commands). This file covers only **where the work stopped** and **what to do next**.

## Where we are
Three strategy tracks, all net of fees and slippage, contract-exact on 1-minute Dhan data:

| Track | Status | Result | Code | Doc |
|---|---|---|---|---|
| Strategy 1: weekly strangle, 1.5× stop | Final script exists | 12.6% CAGR, −32.9% DD (full weeks only) | `final_strategy.py` | `BACKOFF.md` §3 |
| Strategy 2: confirmed credit spread (FII options + gap) | Final script = **v1**; v2 decided but **not coded** | v1 12.4% / −19.1%; v2 11.4% / −8.4% | `final_spread_strategy.py` | `docs/STRATEGY_2_*.md` |
| **Institutional: delta-hedged strangle (D2)** | **Research only, no final script yet** | 26.3% / −14.1% base; 12.3% / −15.4% under pessimistic costs | `research_institutional.py` (`run(..., "D2", band_lots=1)`) | `docs/INSTITUTIONAL_STRATEGIES.md` |
| Institutional: time-staggered strangle (L2) | Research only | 20.2% / −20.1%, same in both periods | `research_institutional.py` (`"L2"`) | same |

The last task finished was the institutional research plus stress tests. Everything is written up in `docs/INSTITUTIONAL_STRATEGIES.md`.

## Next steps (in priority order)
0. **Strategy 5 paper bot (2026-10-20):** `three_sleeve_bot/` is built and tested (145 tests). Deploy is paused until AWS approves the Lambda concurrency increase (request `6ceaaf1faab043ccbb5c05894992bbb04eHF3V11`, 10 → 1000). Then: `python three_sleeve_bot/deploy.py --test`, confirm the SNS alarm email, remove the temporary `TempQuotaAndMetrics` policy from IAM user `dev`, watch the first week, then delete `bear_call_bot/` (its AWS schedules are already removed). Not built yet: `import_backtest.py` (load research trades into Mongo as `src="research"`).
   **Strategy 5 (2026-10-20):** L2 + gap buy + monthly VRP straddle, 27.3% CAGR / −16.9% DD at Rs 10L after the rerun on rebuilt data (`docs/PORTFOLIO_STRATEGY.md`).
   **Strategy 4 (2026-10-20):** stacked strangle + low-VIX gap buy, 20.2% CAGR / −17.2% DD at Rs 10L with running costs (`docs/STACKED_STRATEGY.md`). Target not met below ~Rs 10L.
   **Rs 3L track (2026-10-20):** Strategy 3 gap buy + Strategy 2 v2 50/50 = 13.2% CAGR / −9.3% DD from Jun 2021 (was 19.3% from Jun 2022; Strategy 2 lost in the newly added 2021 weeks) (`docs/OPTION_BUYING.md` §5). Next: paper trade it with `docs/PAPER_TRADING_3L.md`. Monthly VRP≥2 straddle (`docs/MONTHLY_OPTIONS.md`) not yet sized for Rs 3L.
1. **`final_institutional.py` for D2** (delta-hedged ±1× straddle strangle, futures hedge, 1-lot band). Reuse `research_institutional.run` and `Book`. Add:
   - compounding sizing in whole lots (at least 10 lots; below about ₹10L the hedge is too coarse);
   - the ₹1,990/month running costs;
   - futures margin in the capital figure;
   - Monte Carlo (reuse `final_spread_strategy.monte_carlo_trades`);
   - a default futures slippage of 2 points, since 1 point is optimistic;
   - expected rebuild numbers written into the doc.
2. **Same for L2** (staggered strangle): the no-futures alternative for smaller accounts. Also check that L2 combined with delta-hedging doesn't overlap.
3. **Code Strategy 2 v2** in `final_spread_strategy.py`: skip if `credit/(200-credit) < 0.232`, bull puts at half risk (0.04 instead of 0.08). Update `strategies/confirmed_spread.json`, `docs/STRATEGY_2_CONFIRMED_SPREAD.md` §4 and CLAUDE.md with the new expected numbers.
4. **Report:**
   - add an institutional section as `report/section_institutional.html` and register it in `SECTIONS` in `report/build_report.py`;
   - rebuild with `python report/build_report.py`;
   - republish to https://claude.ai/artifact/9zrJVS9fsbgyGPQuMTkk8r (pass it as `url`).
   The published page currently lacks the institutional results and the Strategy 1 entry-rule correction. The local HTML has the correction.
5. **Portfolio question:** combine D2 with Strategy 2 v2 (bear-call-heavy, low correlation?). Measure the weekly return correlation from `data/reports/research_institutional_trades.csv` against `spread_strategy_trades.csv`.
6. **Git:** the repo has **no commits yet** (branch `master`, 28 untracked paths, `.gitignore` in place, about 3.4 MB would be committed). Ask the user before the first commit.

## Open caveats to keep in mind
- **D2 assumptions that could be optimistic:**
  - Futures are priced as spot ± slippage (basis ignored; stress-tested at 5 points a day).
  - Checks run every 5 minutes, not every tick.
  - Delta uses Black-Scholes with Dhan IV.
  - Fixed running costs are excluded (about 1% a year at ₹24L).
- **Gap risk is never hedged.** Apr-2025 tariff week: D2 lost −9.1%. The worst gap in the data (2021+) is smaller than March 2020.
- **Strategy 2's signal choice** used 2024–26 data, so its out-of-sample test is only partly clean. D2's hedge band was *not* tuned; all three bands were reported.
- **The ±500-point strike window:** any leg that drifts outside it is priced at intrinsic value (the engine fix from 2026-10-01). Never forward-fill across that edge.

## How to resume quickly
```bash
python -c "import engine; engine.load_days(rebuild=True)"   # only if data/processed/engine_cache.pkl is missing (~20 s)
python -c "import research_positional as rp, research_institutional as ri; from engine import load_days, load_daily; d,dl=load_days(),load_daily(); c=rp.cycles(dl,d); import pandas as pd; T=pd.DataFrame([r for w,e in c if (r:=ri.run(d,w,'D2',band_lots=1))]); print(ri.summarize(T,'D2'))"
# expected: ALL CAGR 26.3, DD -14.1, IS 32.1, OOS 20.2 (takes ~1-2 min)
```
Stress switches (all off by default): `cost_model.FUT_SLIP_PTS`, `research_institutional.BASIS_PTS_PER_DAY`, `research_institutional.ATM_IV_ONLY`.
