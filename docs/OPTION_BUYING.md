# Option buying research (2026-10-20)

Goal: an option-BUYING strategy to grow a small account (Rs 3L) fast with controlled drawdown,
cheap to run. All results are contract-exact on Dhan 1-min data, net of statutory costs and
slippage; parameters judged on 2021–23, 2024–26 treated as out-of-sample (OOS).

## 1. What failed
| Idea | Script | Result |
|---|---|---|
| Intraday opening-range breakout, buy CE/PE (OR 15/30/60 min, ITM→2 OTM, stops 25/40/60%/OR-mid, targets 50/100/200%/none, breakeven trail) | `research_option_buying.py` | 336 configs. **0%** profitable OOS unfiltered; 1–2% with any filter (noise). |
| Same, expiry day only (0DTE buying) | same | Worked 2021–23 (+15–25%/trade), **dead from 2024** (−9% median OOS). |
| Same, VIX > 15 only | same | Worse: −6.5% median OOS per trade. |
| Long ATM straddle / ±100 strangle, entered 4, 1 or 0 DTE, stop/target grid, filters VIX>15, VIX≤15, IV<RV, VIX up 5% | `research_option_buying_positional.py` | Negative in every filter OOS (median −7% to −20%/trade). Buyers pay the variance risk premium that Strategy 1 earns. |
| Directional weekly buys on momentum, Strategy 2's confirmed signal, always-bull/bear | same | Momentum no better than random; confirmed signal +6% OOS (p 0.08) but 2025–26 negative. |
| Any premium stop on a long option | same | Hurts in every cell: stops cut the winners. |

**VIX > 15 is the wrong filter for buyers**: high VIX means expensive options. Every buying
idea did worse with VIX > 15.

## 2. What survived: Strategy 3, weekly gap-direction buy (`final_gap_buy.py`)
- Full weeks only: at 09:30, 4 sessions before expiry, gap up vs previous close → buy ATM CE,
  gap down → buy ATM PE (nearest weekly).
- No stop. Limit sell at 2× the fill (+100%); else sell 15:15 on expiry day.
- Recommended filter: trade only if VIX at 09:20 ≤ 15 (chosen in-sample: +39.5% vs +8.6%/trade).
- Size: 5% of equity in premium (≈1 lot at Rs 3L), whole lots of 65.

Per-trade (% of premium, net, 1 lot) and permutation test vs random direction over the same weeks:

| Variant | 2021–23 | p | 2024–26 | p |
|---|---|---|---|---|
| All weeks, TP 100% | +12.3% | 0.015 | +13.1% | 0.011 |
| VIX ≤ 15, TP 100% | +14.0% | 0.014 | +23.8% | 0.002 |
| All weeks, no TP | +15.5% | 0.017 | +8.6% | 0.055 |

Sized from Rs 3L (Jan 2021 – Sep 2026, no fixed costs = manual execution):

| Config | CAGR | Max DD | 2021–23 | 2024–26 | MC median CAGR / 5th pct | MC median DD / 1-in-20 DD |
|---|---|---|---|---|---|---|
| VIX≤15, 5% | 16.6% | −13.8% | 6.7% | 28.5% | 19.2% / 2.4% | −24% / −41% |
| VIX≤15, 7.5% | 23.9% | −22.5% | 9.6% | 41.7% | 28.1% / 2.0% | −35% / −56% |
| All weeks, 5% | 15.8% | −28.8% | 17.0% | 14.6% | 21.9% / −2.0% | −37% / −58% |
| All weeks, 10% | 35.4% | −57.5% | 34.5% | 36.3% | 36.0% / −12.2% | −63% / −86% |

Years (VIX≤15, 5%): 2021 −3.0, 2022 −8.6, 2023 +36.9, 2024 +44.0, 2025 +29.2, 2026 YTD +6.7.
Double slippage: VIX≤15 5% → 12.1% CAGR / −16.2% DD. With the Rs 1,990/month algo setup the
5% config drops to ~4% CAGR at Rs 3L, so execute manually (one order + one limit/GTT a week).

## 3. Caveats
- The gap signal was already flagged by Strategy 2's research (which saw 2024–26), so the OOS
  is only semi-clean. The TP choice of 100% vs none was made with OOS visible; no-TP also
  passes but with deeper drawdowns.
- Only ~20 trades a year with the VIX filter; 2021–22 were losing years. Paper trade first.
- Lot granularity at Rs 3L is coarse (1–2 lots), so results are path dependent.
- "Very high CAGR with controlled drawdown" was not found: 25–35% CAGR comes with expected
  drawdowns of 35–60%.

## 4. Data trap found
2025-10-21 (Diwali Muhurat) has no 09:30 option prices; `Day.px` falls back to intrinsic
(0.05 for OTM/ATM), so a long leg "bought" there shows a 300× gain. `run_cycle` only guards
short legs (`px < 1.0`). Long-leg research must skip days with no prices at the entry minute.

## 5. Rs 3L portfolio: gap buy + Strategy 2 v2 (`research_buy_spread_combo.py`)
Sleeve A = Strategy 3 (VIX ≤ 15, 5%); sleeve B = Strategy 2 v2 (skip credit < 23.2% of max loss,
bear calls 8% risk, bull puts 4%). Each sleeve compounds on its own share; manual execution, no
fixed costs. Period Jun 2021 – Sep 2026 (rerun 2026-10-20 after participant OI was extended to 2021;
the first run, Jun 2022+, gave 50/50 = 19.3% / −9.3%).

| Mix (A/B) | CAGR | Max DD | Sharpe | Worst week | 2021–23 | 2024–26 |
|---|---|---|---|---|---|---|
| 100/0 | 17.9% | −13.8% | 1.27 | −5.0% | 7.9% | 28.5% |
| 70/30 | 15.2% | −11.1% | 1.37 | −4.1% | 6.3% | 24.5% |
| **50/50** | **13.2%** | **−9.3%** | **1.46** | **−3.7%** | 5.3% | 21.5% |
| 30/70 | 11.1% | −8.6% | 1.55 | −4.4% | 4.2% | 18.2% |
| 0/100 | 7.5% | −9.7% | 1.18 | −6.1% | 2.6% | 12.5% |

50/50 by year: 2021 −4.6, 2022 +2.7, 2023 +16.5, 2024 +20.9, 2025 +27.9, 2026 YTD +10.1.
- Weekly return correlation 0.47 on the 42 weeks both traded: both use the opening gap.
- Both sleeves lost in 2021; with the longer history Strategy 2 adds smoothness but little
  return. At Rs 1.5L per sleeve each trade is usually 1 lot, so live results will be lumpier.
- Weekly execution checklist: `docs/PAPER_TRADING_3L.md`.
