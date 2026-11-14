# D2 delta-hedged strangle on real prices (2026-11-14) — rejected

Code: `research_d2_realtrade.py` (~15 min). Outputs: `data/reports/d2_realtrade_summary.csv`,
`d2_realtrade_weeks.csv`, `d2_realtrade_account.csv`, log `d2_realtrade_log.txt`.

D2 (sell the ±1× straddle strangle at 09:30 after expiry, hedge delta with Nifty futures every 5 minutes
when it passes 1 lot, out 15:15 on expiry day) was the best structure in INSTITUTIONAL_STRATEGIES.md:
26.3% / −14.1%. That run used `engine.Day.px`, which prices a strike with no traded price at intrinsic
value, the pricing that inflated L2 from PF 1.40 to 1.03 (STRATEGY_6_REALTRADE.md). This reruns D2's
unchanged rules with `research_realtrade.real` prices. Verification only: no parameter chosen, all bands
reported, 2021-23 / 2024-26 split as before.

## Results (weekly return on margin capital, 10 lots, 298 weeks 2021-01..2026-09)

Each row adds one change to the row above it.

| Row | CAGR | Max DD | Sharpe | 2021-23 | 2024-26 | Worst week |
|---|---|---|---|---|---|---|
| R0 old engine pricing, lot 65 (reproduces the doc) | 26.4% | −14.1% | 1.85 | 32.1% | 20.6% | −9.1% |
| **R1 real prices only** | **13.6%** | −20.3% | 0.99 | 23.0% | **4.2%** | −9.5% |
| R2 + NSE lot size of the date | 13.2% | −20.3% | 0.96 | 22.6% | 3.8% | −9.5% |
| R3 + 11.6% margin (Dhan's real) instead of 9% | 10.3% | −16.0% | 0.96 | 17.3% | 3.2% | −7.3% |
| R2, hedge band 2 lots | 10.5% | −23.0% | 0.71 | 18.4% | 2.5% | −11.4% |
| R2, hedge band 4 lots | 4.3% | −43.7% | 0.31 | 15.6% | −6.8% | −15.5% |
| R2, futures slippage 2 pts | 8.6% | −21.2% | 0.66 | 17.0% | 0.2% | −9.5% |
| R2, futures slippage 3 pts | 4.2% | −23.3% | 0.36 | 11.6% | −3.4% | −9.6% |
| R2, basis drift 5 pts/day | 3.7% | −24.0% | 0.32 | 10.8% | −3.6% | −9.7% |
| R2, all three (slip 2 + basis 5 + ATM IV) | 0.4% | −26.2% | 0.10 | 6.2% | −5.7% | −12.8% |

R2 by year: 2021 +30.0%, 2022 +31.9%, 2023 +7.1%, 2024 +2.2%, 2025 +18.4%, 2026 −8.4%. 153 leg exits
(of about 600) had no traded price and were bought back at max(last real price, intrinsic) + urgent
slippage; one week was skipped (no ATM price at 09:30).

**Accounts** (compounding, whole lots of the date at 11.6% margin and 60% use, Rs 1,990 a month):

| Account | CAGR | Max DD | Average month | 2024+ average month | Worst month |
|---|---|---|---|---|---|
| Rs 10L base | 3.3% | −19.4% | Rs 2,965 | Rs 1,248 | −Rs 1.41L |
| Rs 15L base | 7.3% | −16.3% | Rs 10,743 | Rs 2,439 | −Rs 2.59L |
| Rs 25L base | 5.7% | −15.6% | Rs 13,481 | Rs 3,912 | −Rs 3.13L |
| Rs 10L / 15L / 25L, slip 2 + basis 5 | −7.1% / −1.6% / −3.7% | −41% / −26% / −33% | negative | negative | |

The accounts trade 3-22 lots a week (fewer from 2025, when the lot went to 75 then 65 at Nifty
~25,000), so the 1-lot band is coarse for most of the period and they do worse than the 10-lot rows.

## Findings
1. **Half of D2's return was the intrinsic-value pricing.** On real prices it is 13.6%, and 2024-26 is
   4.2% (Sharpe 0.34) against 20.6%: the same bias found for L2. The far strangle strikes D2 sells
   drift out of the stored ±500-point window or stop trading, and the old engine valued them at
   intrinsic (near zero) when they were still worth something.
2. **No cost case leaves a 2024-26 edge.** Futures slippage of 2 points or a basis drift alone takes
   2024-26 to about zero or below; at Dhan's real margin it is 3.2%.
3. **Not a candidate at any account size.** Rs 25L earns about Rs 3,900 a month on base costs since
   2024, and loses money with realistic futures costs.
4. The COVID synthetic stress (`python research_d2_realtrade.py syn`, needs scipy) was not run: D2
   already fails on real 2021-26 prices.

## Verdict
Rejected. No `final_institutional.py`. INSTITUTIONAL_STRATEGIES.md's D2 numbers are synthetic-priced,
like Strategy 5's; Strategy 6 remains the only structure here checked on real prices.
