# Strategy 2: trade analysis (2024–2025 and Dec 2025 – Apr 2026)

Generated 2026-10-01 on the corrected engine (see "Engine fix" below). Data: `data/reports/spread_trades_enriched.csv` (one row per trade, with context) and `data/reports/spread_weeks_all.csv` (every week, including skipped ones). Script: `research_trade_analysis.py`.

## Engine fix that changed the numbers
When a strike drifted outside Dhan's ±500-point window, `engine.py` forward-filled its last price. A deep in-the-money short option kept a stale price: on 2026-03-24 the 23550 PE showed ₹515 against an intrinsic value of ₹667. Strikes outside the window now use intrinsic value.

| | Before | After |
|---|---|---|
| Strategy 1, CAGR / DD | 11.1% / −33.0% | 12.6% / −32.9% |
| Strategy 2, CAGR / DD | 18.0% / −12.9% | 12.4% / −19.1% |
| Strategy 2, 2024–26 CAGR | 18.1% | 9.0% |

The Strategy 2 signal is still significant after the fix: agree-rule p = 0.011 in 2022–23 and 0.011 in 2024–26.

## Period results
| Window | Weeks / traded | Return | Win rate | Max DD | Direction right | Weeks Nifty rose |
|---|---|---|---|---|---|---|
| 2024–2025 | 105 / 56 | +50.0% | 87.5% | −10.1% | 68% | 51% |
| Dec 2025 – Apr 2026 | 22 / 11 | **−11.0%** | 63.6% | −19.1% | 45% | 27% |
| All, 2022–2026 | 295 / 119 | +64.9% | 84.0% | −19.1% | 66% | 53% |

### 2024–2025: what lost
Seven losses, in three clusters:
- **Mar–May 2024, before the election** (VIX 14 → 21). Signals flipped back and forth while the market chopped. A bull put lost to a −2.2% week and a bear call to a +2.6% week. May 2024 was −9.1%.
- **2024-09-27, bull put at the top** (Nifty 2.8% above EMA20). The −3.7% week that began the Oct-2024 FII selloff cost −5.2%.
- **2025-05-09, bear call during the India–Pakistan conflict** (VIX 21.8). The ceasefire rally of +4% cost −4.5%.

### 2024–2025: what made money
Long winning streaks happened when the signal lined up with a persistent flow:
- **Jun–Sep 2024:** 11 straight bull-put wins in a steady uptrend (VIX ~14).
- **Oct 2024 – May 2025:** 15 straight wins, +28%. FIIs were selling, and 2 of 3 trades were bear calls.
- **Jun–Aug 2025:** 9 straight wins at calm VIX 12–15.

### Dec 2025 – Apr 2026: why it lost
- 10 of 11 trades were bull puts while Nifty fell in 73% of weeks. The direction was right only 45% of the time.
- **Feb 2026:** VIX was 11.5–12.6, so the straddle was small and the short strike sat only 0.38–0.58% (about 95–150 points) away. Ordinary 1–1.2% dips caused near-max losses (−4.8%, −3.7%).
- **Mar 2026:** two bull puts were entered on gap-up relief bounces inside a steep downtrend (Nifty 3.6–3.9% below EMA20, VIX 19–25). They lost −7.1% (the worst trade ever) and −5.0%.
- Dec 2025, Jan 2026 and Apr 2026 were profitable. The single bear call in the window won.

## Findings across all 119 trades
1. **Every loss (19 of 19) was a wrong-direction call with a large move.** Losing weeks had a median move of 1.58%, winning weeks 0.85%. Quiet weeks never lost.
2. **Bear calls carry the strategy.**
   - Bear calls: 51 trades, 90% win rate, +0.94% average.
   - Bull puts: 68 trades, 79% win rate, +0.08% average; they caused 71% of all losses.
   - Bear calls were better in both periods (2022–23: +0.68% vs +0.30%; 2024–26: +1.12% vs −0.03%). Nifty falls faster than it rises, so bull put max-loss weeks are the sharp selloffs, and gap-ups inside downtrends are often relief bounces.
3. **Profitable months** had the direction right 82% of the time, VIX averaging 13.9, and moves at or below the implied move. **Losing months** had the direction right 33% of the time and VIX averaging 15.0.
4. **Filters checked and rejected**, because they did not hold in both periods (adding them would be fitting to hindsight):
   - skip VIX > 18: 2022–23 averaged +0.9%, 2024–26 averaged −2.2%;
   - no bull puts below EMA20: worked in 2022–23, backfired in 2024–26;
   - minimum cushion size: no consistent pattern.

## Sizing variants (₹5L, corrected engine, 2022–2026)
| Variant | CAGR | Max DD | Sharpe | Dec 25 – Apr 26 |
|---|---|---|---|---|
| Current (bull put and bear call, full size) | 12.4% | −19.1% | 1.46 | −11.0% |
| Bull puts at half size | 10.2% | −10.1% | 1.63 | −4.9% |
| **Bear calls only** (51 trades) | 10.9%* | **−6.9%** | **2.91** | **+1.8%** |

\*Running costs are charged only in months that have a trade, which flatters bear-only by about 1 CAGR point.

The bear-only rule is significant on its own in both periods (p = 0.037 and 0.010). Recommendation: trade bear calls at full size and bull puts at half size or not at all. This is a sizing change backed by both periods, not a new signal.

## VIX, reward-to-risk and "quiet then explode" filters (2026-10-01)
Scripts: `research_vix_regime.py` (diagnostics) and `research_vix_filters_bt.py` (sized tests). Every threshold is a tercile cut from 2021–23, applied unchanged to 2024–26.

**Do quiet markets explode?** Across all 295 weeks:
- **Compressed ranges** (5-day average range / 60-day average range below 0.82) moved more than options priced in 40% / 53% of weeks (2021–23 / 2024–26), against 29% / 41% after wide-range weeks. This is the only regime measure consistent in both periods.
- **Not consistent:** VIX level, VIX 1-year percentile, VIX rising over 5 days, Bollinger squeeze, RV5/RV20, VIX/RV.

**Trading the explosion (buy ATM straddle in compressed weeks): rejected.**
- In normal weeks, buying straddles loses about ₹2,000 per lot per week.
- In compressed weeks it roughly breaks even; with a 50% target it makes +₹585 / +₹435 per lot. That is not significant (p = 0.11 and 0.08).
- Sized with running costs: −1.7% CAGR.
- Compression is also not a useful skip filter for either strategy.

**VIX entry filters: rejected.** No VIX measure improved either strategy in both periods.

**Reward-to-risk filter for Strategy 2: adopted.** Skip the trade if the credit is less than 23.2% of the max loss (credit < about 38 points on a 200-point spread). Low-credit trades averaged −0.07% / −0.02% per trade in the two periods. Any threshold from 0.20 to 0.28 helps, so this is not one lucky cutoff.

| Version (₹5L, 2022–2026) | Trades | CAGR | Max DD | Sharpe | Dec 25 – Apr 26 |
|---|---|---|---|---|---|
| v1 current | 119 | 12.4% | −19.1% | 1.46 | −11.0% |
| Reward-to-risk ≥ 0.232 | 91 | 12.5% | −15.8% | 1.88 | −8.9% |
| **Reward-to-risk ≥ 0.232 + bull puts at half size** | 91 | 11.4% | **−8.4%** | 2.31 | −4.2% |
| Reward-to-risk ≥ 0.232 + bear calls only | 48 | 12.5%* | −5.3% | 3.88 | +1.7% |

\*Bear-only is charged running costs in fewer months, which flatters its CAGR by about 1 point. It also has only 48 trades.

**Recommended Strategy 2 v2:** reward-to-risk ≥ 0.232, bear calls at full size, bull puts at half size.
