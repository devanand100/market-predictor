# "Delta-Rotation Credit Spread Expiry" (2026-11-15): does not reproduce, rejected

    python research_delta_rotation.py     # about 1 minute; data/reports/delta_rotation/

**Source:** a pasted marketing description and metric card.

- **Description:** an "alpha" built from IV, curvature, "Hamiltonian", eigenvalues, entropy and
  predicted volatility; its momentum; and "alpha2" from spot returns and IV-curvature changes. A
  bullish signal sells an ATM put spread and a bearish signal an ATM call spread. Exits are a
  target at 50% of maximum profit, a stop at a share of margin, and a time exit. One position at
  a time, within trading hours.
- **Claimed metrics:** about 2 trades a week, 54.69% win rate, average win +6.34%, average loss
  −5.07%, max drawdown −29.12%, volatility 68.98%, Sharpe 1.89, Calmar 4.17. That implies about
  +1.2% per trade and about 120% a year.

The description has no formulas, so the product cannot be backtested. This tests one fixed
reading of it, set before any P&L was seen (full rules in the script docstring).

## Reading tested

- **Data:** Dhan 1-minute Nifty weekly options, 2021-01 to 2026-10.
- **Contracts and prices:** legs are fixed by absolute strike. Every minute is marked at its
  close. Slippage is `0.3 + 0.5%` of premium per fill, and `0.5 + 1.5%` on stops. Charges come
  from `cost_model`, by date.
- **Timing:**
  - Checkpoints every 30 minutes, 10:00 to 14:30.
  - The signal is read from bar m−1 and filled at bar m.
  - No new entry on expiry day.
- **Smile:** Dhan IV on the out-of-the-money side, with the put at ATM−200 and the call at
  ATM+200.
  - skew = put IV − call IV
  - curvature = mean of the two wings − ATM IV
- **alpha** = z(spot return since 09:30) − z(skew change since 09:30).
- **Momentum:** the change in alpha since the previous checkpoint.
- **alpha2:** z(30-minute return), counted only when curvature did not rise.
- **"Energy" gate:** z(r30)² + z(ΔATM IV30)² ≥ 2. Eigenvalues and entropy are undefined in the
  description and left out.
- **Bull signal:** alpha > th, momentum > 0 and alpha2 > 0. Sell the ATM put and buy a put W
  points lower. Bear is the mirror image.
- **Hedge leg:** the text says to buy an ITM put. Paired with a sold ATM put, that is a debit
  spread, so the OTM put is used.
- **Exits:**
  - Target: 50% of the credit.
  - Stop: a loss of `stop` × margin, with margin = W × quantity.
  - Time: 15:15 on the same session (`hold` 0) or the next one (`hold` 1), never past expiry-day
    15:15.
- **Grid** (32 cells): th ∈ {0.5, 1}, W ∈ {100, 200}, stop ∈ {0.3, 0.5}, hold ∈ {0, 1},
  gate ∈ {off, on}.
- **Rule choice:** frozen on the best 2021-23 profit per trade as a share of margin, among cells
  with at least 1 trade a week. The out-of-sample period is 2024 to 2026-10.
- **Account:** ₹5L, fresh in each period. Lots = 25% of equity / (W × lot), compounding. Trading
  stops at 10% of the starting capital. At 100% of margin, a run from 2021 went to zero within
  2021-23.
- **Per-trade figures** are computed on every trade at one lot, net of slippage and one-lot
  charges, as a share of margin (W). The card's +6.34% / −5.07% has no stated base, so it is
  compared on that base too. Its reward:risk and win rate do not depend on the base.

## Results

- **No cell makes money, in either period.** All 32 lose per trade, by −2.8% to −12.8% of margin
  in 2024-26. The best cell in 2021-23 makes −0.7%.
  - Before slippage and charges the trades are about flat. In two cells checked by hand they
    make −0.4 to −2.7 points per trade on credits of 40 to 70.
- **The frozen rule against the claim.** The frozen rule is th 0.5, W 200, stop 30% of margin,
  hold 1, energy gate on.

| Metric | Claim | 2021-23 | 2024-26 (OOS) |
|---|---|---|---|
| Trades a week | 2 | 1.14 | 1.10 |
| Win rate | 54.7% | 60.2% | 54.0% |
| Average win / loss, % of margin | +6.3% / −5.1% | +15.8% / −25.6% | +16.1% / −27.3% |
| Reward : risk | 1.25 | 0.62 | 0.59 |
| Profit per trade, % of margin | +1.2% | −0.7% | **−3.9%** |
| Profit factor | about 1.5 | 0.93 | 0.69 |
| Max wins / losses in a row | 7 / 5 | 6 / 5 | 5 / 4 |
| CAGR (25% of equity as margin) | about 121% | −6.7% | −37.7% |
| Max drawdown | −29% | −60% | −81% |
| Volatility | 69% | 38% | 42% |
| Sharpe / Sortino (daily, no risk-free) | 1.89 / 3.18 | 0.01 / 0.02 | −0.89 / −1.04 |
| Winning days / months / quarters | 56% / 69% / 80% | 57% / 44% / 58% | 50% / 26% / 33% |

- **Controls, at the same entry checkpoints (per trade, share of margin):**

  | Control | 2021-23 | 2024-26 |
  |---|---|---|
  | Reversed signal | −6.5% | −1.0% |
  | Always bull | −3.5% | −4.0% |
  | Always bear | −2.9% | −1.0% |

  - Random direction, 200 runs, before charges: the rule makes +0.4% of margin per trade in
    2021-23, against a random median of −2.6%; it beats 97% of the runs.
  - In 2024-26 the rule makes −2.7%, against a random median of −1.2%; 79% of the runs do as
    well or better.
  - The direction signal looked real in the design years and failed out of sample. The reversed
    signal did better than the rule out of sample.
- **The payoff shape works against the claim.**
  - An ATM spread with a 50%-of-credit target and a stop wins small and loses large: its
    reward:risk is about 0.6, against the claimed 1.25.
  - Stops fill beyond their level. In the W 100 / stop 30-point cell checked by hand, stopped
    trades lost 40 points on average in 2021-23 and 45 in 2024-26, from overnight gaps and stop
    slippage.
  - To reach +1.2% per trade with the frozen rule's wins and losses, the win rate would have to
    be about 65% (62% just to break even). The best cell reaches 61.6% in 2021-23 and 58.3% out
    of sample.
- **Fallback bars:** 1 to 21 exits per cell fired on an intrinsic-fallback bar, a gap of more
  than 5 minutes in the data. That is too few to move any result.

## Verdict

Rejected. The targets are not reached, and the rule was not tuned towards them. Under this
reading:
- the win rate is close to the claim, but wins are smaller than losses;
- per trade it loses 0.7% of margin in the design years and 3.9% out of sample;
- with 25% of equity as margin it loses 6.7% and 37.7% a year, with drawdowns of −60% and −81%.

The claimed 69% volatility would need about 40-45% of equity as margin, since this curve shows
about 40% volatility at 25%. With a negative result per trade, more size only loses faster.

**Limits:**
- Another reading of the undefined terms ("Hamiltonian", eigenvalues, entropy, "predicted
  volatility") could behave differently. But nothing in the grid made money in either period,
  and the structure itself (ATM spread, 50% target, stop on margin) is about break-even before
  costs.
- Margin is taken as W × quantity, not Dhan's quoted margin for a hedged spread. This changes
  the percentages and the account curve, but not the sign of any result.
- Recovery times are not meaningful on curves that never regain their peak.
