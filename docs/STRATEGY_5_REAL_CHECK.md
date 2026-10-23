# Strategy 5 (three_sleeve_bot) on real prices: how much edge was synthetic

Checked 2026-10-21 with the real-market replay (`research_realtrade.py`). Scripts:
`research_realtrade_s5.py` (per-sleeve ladder), `research_monthly_realonly.py` (monthly sleeve on real
prices only), `research_realtrade_s5_portfolio.py` (whole account). The bot's own rules are used: L2 stop
checks every 5 minutes with at least 3 lots, gap buy on VIX(09:20) ≤ 15 with a 5-minute close ≥ 2× fill
target, and the monthly straddle on VRP ≥ 2.

## Whole account (running costs on)

| Capital | Research booking (`final_portfolio_strategy.py`) | Replay, old synthetic pricing | Replay, real prices only |
|---|---|---|---|
| ₹10L | 27.3% / −16.9% / PF 1.60 | 22.7% / −22.0% / 1.52 | **17.7% / −18.7% / 1.42** |
| ₹15L (bot) | 29.4% / −17.1% / 1.59 | 26.8% / −20.4% / 1.55 | **19.3% / −20.7% / 1.41** |
| ₹25L | 31.1% / −17.3% / 1.60 | 29.7% / −21.0% / 1.57 | **22.0% / −21.0% / 1.42** |

Each cell is CAGR / max drawdown / trade profit factor. At ₹15L the real-price figures by year are
2021 +2.6%, 2022 +1.0%, 2023 +31.1%, 2024 +39.0%, 2025 +31.7% and 2026 +10.7%. In-sample (2021–23)
it makes 10.8% with profit factor 1.29.

At ₹15L the research figure drops by 10.1 CAGR points in total:
- **About 2.6 points come from realistic account handling,** not synthetic prices. These are the actual lot size of each date, drawdown on daily mark-to-market, and profit factor per tranche rather than per week.
- **About 7.5 points are synthetic edge,** that is, prices that never traded.

## Per sleeve (per unit, sizing-free)

| Sleeve | Research | Replay, synthetic | Replay, real | Synthetic share |
|---|---|---|---|---|
| L2 staggered strangle (per week) | 10,403 pts, PF 1.40 | 11,108 pts, PF 1.43 | 7,391 pts, PF 1.27 | **≈ 33% of points** |
| Gap buy | 2,988 pts, PF 1.55 | 3,085 pts, PF 1.56 | 3,085 pts, PF 1.56 | **0%** |
| Monthly straddle, VRP ≥ 2 (₹ per lot) | ₹2,88,179, PF 3.91 | – | ₹1,50,484, PF 1.70 (conservative) | **up to ≈ 48%** |

- **L2:** the old engine priced a strike with no trade at intrinsic value, near zero for OTM shorts, so tranches on gappy days (e.g. Budget Sunday 2026-02-01) never stopped. The synthetic-pricing replay reproduces the research within 7%, which validates the simulator.
- **Gap buy:** an ATM long almost never leaves the stored window, so pricing makes no difference. The bot's 5-minute-close target earns about 16% more than a resting limit at 2× (3,085 vs 2,600 pts). That is a real, tradable rule.
- **Monthly:** 12 of the 40 trades exit at a strike with no traded price (parity or Black-76 in research). Only those trades differ: −₹44k per lot with model prices, −₹182k on the conservative real-only rule (last real price plus the change in intrinsic value). The truth lies between, so the synthetic share is somewhere between 0% and 48%.

## Consequences
- The bot trades on live LTP, so its paper P&L will be real. Expect about **19% CAGR with profit factor about 1.4** at ₹15L, not 27–29%. 2021–22 were flat on real prices.
- Strategy 5 misses the profit-factor ≥ 1.5 target on real prices. Strategy 6 (`docs/STRATEGY_6_REALTRADE.md`) meets all targets on the same replay.
- Reusing the bot for Strategy 6 means replacing `rules.py` and its tests. The infrastructure stays: schedule, Dhan, Mongo, Discord, calendar.

## At Dhan's real margin (2026-10-23, `research_realtrade_s5_real_margin.py`)
The figures above size L2 (30%) and the monthly straddle (20%) on the 9% margin model. Dhan asks ~11.6% of notional.
At that rate the model's lots tie up ~39% + ~26% of equity (~45% for L2 on expiry day) + 3% gap premium, under the
90% cap, so **the real-price figures above stand as they are**. If the weights are instead read as shares of Dhan's
margin (fewer lots):

| Capital | 9% model lots (feasible at Dhan) | Lots sized on Dhan's 11.6% |
|---|---|---|
| ₹10L | 17.7% / −18.7% / 1.42 (2024+ 29.5%) | 13.7% / −17.3% / 1.53 (2024+ 22.7%; L2 below its 3-lot minimum most weeks) |
| ₹15L (bot) | **19.3% / −20.7% / 1.41** (2024+ 29.3% / −20.7%) | 17.0% / −16.8% / 1.46 (2024+ 27.9% / −14.8%) |
| ₹25L | 22.0% / −21.0% / 1.42 (2024+ 32.9%) | 17.9% / −18.7% / 1.42 (2024+ 29.4%) |

CAGR / max drawdown / trade PF, real prices only, running costs on, Jan 2021 – 1 Oct 2026. 2021–22 stay flat or
negative in every case. Strategy 6 at ₹5L on the same basis: 34.8% / −13.3% / 1.77.
