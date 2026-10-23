# Strategy 5: three-sleeve portfolio (2026-10-20)

Goal: 25–35% CAGR with a controlled drawdown. Code: `research_portfolio_sleeves.py` (search),
`final_portfolio_strategy.py` (lot-exact, event-driven backtest).

## Sleeves (one account)
| Sleeve | What | Size | Entry | Stop | Exit |
|---|---|---|---|---|---|
| **L2** weekly staggered strangle | Sell CE at ATM+S, PE at ATM−S (S = ATM straddle), 1/3 of the lots on each of the first 3 sessions after expiry | 30% of equity as margin (≥ 3 lots) | 09:30 on days 1, 2, 3 | Each tranche: loss ≥ 1.5× its credit | 15:15 on expiry day |
| **GAP** gap buy (Strategy 3) | ATM CE if gap up, PE if gap down, full weeks, only VIX(09:20) ≤ 15 | 3% of equity in premium | 09:30, 4 sessions before expiry | None | Limit sell at 2×, else 15:15 expiry day |
| **MON** monthly straddle | Sell ATM CE + PE of the next monthly expiry, only if VIX − 20-day RV ≥ 2 (previous session) | 20% of equity as margin | ~45 DTE | None | 21 DTE |

Planned margin: 50% of equity; the rest is a buffer.

## Why it works
- The two short-volatility sleeves earn the variance premium on different expiries and timing.
  Weekly correlation is 0.37.
- The gap buy is long gamma and directional, and is negatively correlated with the short-vol
  sleeves (−0.20): it pays in the trending weeks that stop out the strangle.
- The weights were chosen on 2021–23 only: highest IS CAGR with IS max DD better than −12%,
  margin ≤ 60% (`data/reports/portfolio_sleeves_grid.csv`).

## Results (lot-exact, net of statutory costs and slippage)
Rerun 2026-10-20 after the L2 and monthly research were regenerated on the rebuilt data (mock
Saturdays removed); the first run gave 26.8% / −16.3% at Rs 10L.

| Start | Running costs | CAGR | Max DD | Sharpe | 2021–23 | 2024–26 | Worst week |
|---|---|---|---|---|---|---|---|
| Rs 3L | Rs 1,990/mo | 2.3% | −29.0% | 0.33 | −3.4% | 8.9% | −5.3% |
| **Rs 10L** | Rs 1,990/mo | **27.3%** | **−16.9%** | 1.42 | 18.4% | 37.8% | −6.1% |
| Rs 25L | Rs 1,990/mo | 31.1% | −17.3% | 1.47 | 21.7% | 42.1% | −6.9% |

Rs 10L by year: 2021 +9.2, 2022 +12.4, 2023 +35.0, 2024 +40.8, 2025 +46.0, 2026 YTD +17.9.
Every year is positive, unlike Strategies 1–4.
P&L share at Rs 10L: gap buy 46%, L2 30%, monthly 24%.

**Live sizing at today's index level:** at Nifty ~25,000 one strangle lot needs ~Rs 1.47L margin,
so the 30% L2 sleeve needs ~Rs 14.7L to reach the 3-lot minimum. The backtest averaged 4 lots at
Rs 10L only because Nifty was 15–18k in 2021–23. The paper bot (`three_sleeve_bot/`, removed 2026-10-23, see `THREE_SLEEVE_BOT.md`) ran Rs 15L.
Monte Carlo (fractional weekly returns, 5 years): median CAGR 32.7%, median max DD −19.4%,
1-in-20 max DD −31.9%.

At Rs 3L neither short-vol sleeve can be sized (L2 needs 3 lots, about Rs 4.4L of margin, at
30%), so only the gap buy trades and fixed costs dominate.

## Caveats (read before trusting the numbers)
- **2024–26 was unusually good for every sleeve.** Treat the 2021–23 result (18–22% CAGR) as the
  more realistic forward estimate.
- **Monthly sleeve data:** 60-minute data; about 38% of exits are priced by put-call parity or
  Black-76 because the strike left Dhan's window. The VRP ≥ 2 filter was chosen with the whole
  sample visible.
- **L2:** per-lot P&L is the 10-lot research result divided by 10, and L2 trades include
  holiday weeks. Its stops need an algo.
- **Gap signal:** OOS only semi-clean (`docs/OPTION_BUYING.md`).
- **Tail risk:** short gamma on two sleeves; a March-2020 gap is not in the data. Worst week
  seen −5.9%.
- **Margin:** SPAN margins can rise sharply in a crisis. The 50% buffer is there for that; do
  not size up the weights.
