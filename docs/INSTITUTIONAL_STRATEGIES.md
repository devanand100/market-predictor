# Institutional short-volatility structures: findings (2026-10-01)

Code: `research_institutional.py` (14 variants), `research_institutional_stress.py` (stress tests).
Outputs: `data/reports/research_institutional*.csv`.

## Setup
- **Timing:** enter 09:30 on the first session after expiry, **every** week (294 weeks, including holiday weeks). Exit 15:15 on expiry day.
- **Pricing:** contract-exact on 1-minute data, checked every 5 minutes.
- **Size:** 10 lots (650 units), so a 1-lot futures hedge is a fine adjustment.
- **Capital:** 9% of notional per strangle at 60% margin use, the same rule as Strategy 1, about ₹24L at Nifty 25,000.
- **Costs:** all fees and slippage included; futures fill at spot ± 1 point. Fixed running costs are excluded (about 1% a year at this capital).
- **Delta:** Black-Scholes using the Dhan per-strike IV.

## Results
| Variant | CAGR | Max DD | Sharpe | 2021–23 | 2024–26 | Worst week |
|---|---|---|---|---|---|---|
| S0 strangle ±1× straddle, 1.5× stop (Strategy 1 rules, every week) | 16.9% | −33.8% | 0.68 | 13.1% | 21.3% | −18.4% |
| Naked short straddle | 10.2% | −66.6% | 0.46 | 6.9% | 14.0% | −26.9% |
| Naked short strangle | 18.1% | −44.9% | 0.66 | 15.3% | 21.4% | −20.4% |
| L1 multi-level ladder (½, ¾, 1× straddle) | 12.9% | −40.0% | 0.52 | 11.0% | 15.1% | −19.3% |
| **L2 time-staggered strangle** (⅓ entered on each of days 1–3) | **20.2%** | **−20.1%** | 1.05 | 19.9% | 20.5% | −7.7% |
| D1 straddle, delta-hedged, 1-lot band | 16.4% | −33.7% | 0.89 | 32.9% | 0.2% | −20.3% |
| D1 straddle, delta-hedged, 2-lot band | 21.3% | −29.5% | 1.05 | 31.9% | 10.4% | −21.9% |
| D1 straddle, delta-hedged, 4-lot band | 16.6% | −31.4% | 0.76 | 19.9% | 13.0% | −16.5% |
| **D2 strangle, delta-hedged, 1-lot band** | **26.3%** | **−14.1%** | **1.84** | 32.1% | 20.2% | −9.1% |
| D2 strangle, delta-hedged, 2-lot band | 23.3% | −16.2% | 1.48 | 27.4% | 19.0% | −11.0% |
| D2 strangle, delta-hedged, 4-lot band | 16.5% | −25.7% | 0.85 | 24.4% | 8.2% | −15.1% |
| D3 strangle, roll untested leg when 2× imbalance | 11.7% | −49.3% | 0.48 | 3.4% | 21.7% | −18.1% |
| G1 long straddle, gamma scalping (1-lot band) | −61.6% | −99.5% | — | −64.1% | −58.5% | — |

## Stress tests: D2 delta-hedged strangle
| Case | CAGR | Max DD | 2021–23 | 2024–26 |
|---|---|---|---|---|
| Base (10 lots, 1-lot band, 1-pt futures slippage) | 26.3% | −14.1% | 32.1% | 20.2% |
| Futures slippage 2 pts | 21.2% | −15.1% | 26.0% | 15.9% |
| Futures slippage 3 pts | 16.2% | −16.0% | 20.3% | 11.8% |
| Basis drift 5 pts/day charged against the hedge (pessimistic) | 15.7% | −16.0% | 19.4% | 11.6% |
| Deltas from ATM IV only | 28.2% | −13.3% | 33.4% | 22.6% |
| **All three together** | **12.3%** | −15.4% | 14.5% | 9.8% |
| 2-lot band + 2-pt slippage + basis | 10.4% | −18.5% | 12.4% | 8.2% |
| 4 lots (~₹10L), 1-lot band | 17.1% | −13.6% | 16.2% | 18.1% |
| 4 lots + 2-pt slippage + basis | 5.9% | −15.4% | 3.5% | 8.6% |

D2 by year (base case): 2021 +41.6%, 2022 +49.2%, 2023 +8.6%, 2024 +17.9%, 2025 +32.4%, 2026 YTD +4.4%.

## Findings
1. **Delta-hedging the strangle with futures is the best structure found in this project.** Base: 26.3% CAGR, −14.1% drawdown. Pessimistic costs: 12.3% CAGR, −15.4% drawdown, which matches Strategy 1's return with less than half its drawdown.
   - Tight hedging (a 1–2 lot band on a 10-lot position) matters: the 4-lot band fades to 8% in 2024–26.
   - Each extra point of futures slippage costs about 5 CAGR points, so execution quality decides the outcome.
2. **Delta-hedging a straddle does not work as well.** It was excellent in 2021–23 but fell to 0–13% in 2024–26. The straddle's gamma is too high, so it hedges constantly, and the higher futures STT from Oct 2024 made that expensive.
3. **Time-staggered entry (L2) is the best unhedged structure.** 20.2% CAGR, −20.1% drawdown, nearly identical in both periods, −7.7% worst week, with no futures needed. Spreading the entry over three days diversifies the entry price.
4. **Multi-level ladders (L1) are worse than a single strangle.** The closer strikes add gamma risk without adding enough premium.
5. **Rolling the untested leg (D3) is the worst adjustment:** −49% drawdown. It moves short strikes toward the market in exactly the trending weeks that hurt.
6. **Gamma scalping (long straddle + hedge) loses about 60% a year.** Weekly Nifty options are consistently priced above the moves they cover. Selling that premium is the edge; buying it is the cost.
7. **Gap risk is not removed by hedging.** In the Apr-2025 tariff-crash week, D2 lost −9.1% (it hedged after the gap and was whipsawed by the rebound). Naked short strangle: −7.3%. The strangle with a stop: −18.4%, because the stop sold at the gap low.
8. **Strategy 1's rule only trades full 5-session weeks** (it enters 4 sessions before expiry). That skipped the Apr-2025 crash week. Counting every week, the same rules give −33.8% drawdown and a −18.4% worst week.

## Practical constraints for D2
- **Capital:** about ₹24L for 10 lots. At 4 lots (about ₹10L) the 1-lot futures hedge is coarse: the base case gives 17.1%, but pessimistic costs drop it to 5.9%. Nifty has no smaller futures contract, so below about ₹10L this structure is not practical.
- **Futures margin:** about ₹1.5–2L per lot. Under SPAN a hedge usually lowers total portfolio margin, but keep a buffer.
- **Workload:** 4–17 hedge trades a week. This needs an algo (broker API), not manual trading.
