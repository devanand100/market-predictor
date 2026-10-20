# Strategy 4: stacked strangle + low-VIX gap buy (2026-10-20)

Code: `final_stacked_strategy.py` (final), `research_regime_switch.py` (grid of switch / stack
rules). Lot-exact, net of statutory costs and slippage, Jan 2021 – Sep 2026, data after the
2026-10-20 rebuild (participant OI from 2021, mock Saturdays removed).

## Idea
Strategy 1 (sell the weekly strangle) and Strategy 3 (buy the ATM option in the gap direction)
both have an edge and their weekly returns are slightly negatively correlated (−0.17). Running
both in one account, every full week, uses idle capital: the strangle uses margin, the gap buy
a few percent of equity in premium.

## Rules
Every full week at 09:30, 4 sessions before expiry:
1. Strangle (Strategy 1 rules): lots = floor(30% of equity / (9% × spot × 65)). Below ~Rs 5L
   this is 0, so take 1 lot if its margin is ≤ 55% of equity.
2. Gap buy (Strategy 3 rules) only if VIX at 09:20 ≤ 15: 3% of equity in premium.

Sizes were chosen on 2021–23 with fractional lots: highest IS CAGR with IS max DD better than
−22% (`data/reports/stack_grid_fractional.csv`).

## Results
| Start capital | Running costs | CAGR | Max DD | Sharpe | 2021–23 | 2024–26 | MC median CAGR | MC 1-in-20 DD |
|---|---|---|---|---|---|---|---|---|
| Rs 10L | Rs 1,990/mo | **20.2%** | **−17.2%** | 1.57 | 10.5% | 31.7% | 20.1% | −25.9% |
| Rs 10L | none | 21.7% | −16.9% | 1.67 | 11.8% | 33.5% | 21.6% | −25.1% |
| Rs 6L | Rs 1,990/mo | 14.7% | −20.2% | 1.31 | 3.8% | 27.9% | 14.5% | −26.1% |
| Rs 3L | Rs 1,990/mo | 14.2% | −30.5% | 1.02 | 5.5% | 24.5% | 14.1% | −37.1% |
| Rs 3L | none | 19.0% | −23.4% | 1.45 | 11.9% | 27.2% | 18.9% | −28.0% |

Rs 10L by year: 2021 −5.4, 2022 −2.3, 2023 +45.6, 2024 +31.1, 2025 +42.6, 2026 YTD +13.4.

Other rules in `research_regime_switch.py`:
- **Switch** (strangle when VIX > T, gap buy when VIX ≤ T): 22–27% CAGR at Rs 3L but −23% to −35% DD.
- **Full stack** (60% margin + 5% gap): 48.7% CAGR at Rs 3L, but the fractional-lot test gives a −44% DD.
- **VIX/RV ratio switch:** unstable between the two periods.

## Caveats
- **Below about Rs 10L the target is not met.** Lot granularity forces the strangle to about
  50% margin at Rs 3L, and the fixed costs are a large share of a small account.
- 2021 and 2022 were losing years. Almost all the return came in 2023–25.
- The strangle's position-level stop (1.5× credit) needs an algo or a broker basket stop, so
  count the running costs.
- Gap risk on the short strangle is unhedged. The worst week in the data is −5%; March 2020 is
  not in the options data.
- The gap signal's OOS is only semi-clean (see `docs/OPTION_BUYING.md`).
