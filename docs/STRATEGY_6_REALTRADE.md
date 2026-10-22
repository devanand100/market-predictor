# Strategy 6: VIX-regime weekly options portfolio (real-market replay)

Status 2026-10-21. Code: `final_realtrade_strategy.py` (rules and stress tests) on the simulator
`research_realtrade.py`. Research trail: `research_realtrade_grid.py` → `_grid2.py` → `_grid3.py` →
`research_realtrade_portfolio.py` → `research_realtrade_sensitivity.py`.

## 1. Why a new simulator

The goal was CAGR > 20%, profit factor ≥ 1.5 and a controlled drawdown, in a backtest that trades
like a real account, with no parity or model prices. `research_realtrade.py` replays the account
day by day and minute by minute on `data/processed` (engine cache + daily features + raw Dhan daily VIX):

- **Real prices only.** Fills happen only at the contract's own traded 1-minute price (LTP, at most 5 minutes stale inside the ±500 window). There is no put-call parity, no Black-76 or Black-Scholes, and no intrinsic value for a leg that is still trading. A trade opens only if every leg has a real price at the entry minute.
- **Unpriced legs fill conservatively.** If a leg has drifted out of the stored strike window when it must be closed, a short is bought back at max(last real price, intrinsic) plus urgent slippage, and a long is sold at intrinsic. This happened 57 times in 330 trades.
- **Lot sizes follow NSE history.** Each trade uses the lot size in force on its date (75, 50, 25, 75, 65), whole lots only, sized on equity (cash plus open MTM) at the decision minute, with compounding.
- **Margin is capped.** Margin is 9% of notional per naked short lot, or 11% on expiry day from 2024-11-20 (the SEBI exposure-margin add-on). Total margin is capped at 90% of equity.
- **Costs are complete.** Statutory charges apply per order at the traded quantity. Slippage applies to every fill, at the urgent rate on stops. A limit target fills at the limit only when the 1-minute high trades through it. Running costs are ₹1,990 a month.
- **Drawdown includes open losses.** Equity is marked to market at every session close.
- **No look-ahead.** VIX is the previous session's close, the gap is 09:30 spot against the previous close, and VRP uses the previous session's RV20. Parameters were chosen on 2021–2023, and 2024 onward is out-of-sample.

**Key finding:** the earlier short-strangle results are inflated by synthetic prices. On the
2026-02-01 Budget Sunday, 43% of option minutes have no price. The old engine priced those missing
out-of-the-money legs at intrinsic value, near zero, so stops never fired. On real prices, the L2
staggered strangle (Strategy 5's main sleeve) falls from profit factor 1.40 to 1.03 at ₹10L, a loss.
Iron condors, iron flies and most credit spreads lose in nearly every year
(`data/reports/realtrade_grid.csv`). Strategy 5's figures should be treated as optimistic.

## 2. Rules

VIX here means the previous session's India VIX close.

| Sleeve | When | Entry (09:30) | Exit | Size |
|---|---|---|---|---|
| S2 | VIX ≥ 15 | Sell strangle at ATM ± 1.25 × ATM straddle (50-pt strikes, ≤ 450 from ATM), 2 sessions before expiry | Loss ≥ 1.0 × credit (1-min MTM), else 15:15 expiry day | 45% of equity as margin |
| E0 | VIX ≥ 15 | Sell strangle at ATM ± 0.5 × straddle on expiry day | Loss ≥ 0.5 × credit, else 15:15 | 45% of equity as margin |
| GAP | VIX ≤ 13 | Buy ATM CE if 09:30 spot > previous close, else ATM PE, 4 sessions before expiry | Resting limit sell at 2 × fill, else 15:15 expiry day | 5% of equity in premium |

The short sleeves trade only high-VIX weeks and the gap buy only low-VIX weeks, so they rarely run
together. S2 and E0 overlap on expiry day, and the 90% margin cap trims lots when needed.

## 3. Results (₹10L start, running costs on, Jan 2021 – 1 Oct 2026)

| | CAGR | Max DD | Trade PF | PF incl. running | Sharpe | Trades |
|---|---|---|---|---|---|---|
| Full | **38.7%** | **−13.5%** | **1.76** | 1.73 | 1.76 | 330 |
| IS 2021–23 | 49.8% | −13.5% | 2.32 | | | |
| OOS 2024–26 | 27.5% | −13.4% | 1.58 | | | |

Years: 2021 +23.0%, 2022 +45.9%, 2023 +87.0%, 2024 +40.2%, 2025 +27.9%, 2026 (to 1 Oct) +8.7%.
Worst drawdown: 2023-08-09 to 2023-08-30.

Per sleeve (trades, profit factor):

| Sleeve | IS | OOS |
|---|---|---|
| S2 | 88, 2.10 | 34, 1.77 |
| E0 | 92, 2.11 | 43, 1.71 |
| GAP | 33, 2.59 | 40, 1.51 |

The gap buy earns about 65% of the net profit at this size.

## 4. Stress tests (`final_realtrade_strategy.py`)

| Case | CAGR | Max DD | PF | OOS CAGR / PF |
|---|---|---|---|---|
| Base ₹10L | 38.7 | −13.5 | 1.76 | 27.5 / 1.58 |
| Slippage × 2 | 32.4 | −13.6 | 1.63 | 21.7 / **1.45** |
| Slippage × 3 | 28.2 | −13.5 | 1.57 | 19.8 / 1.42 |
| Margin 12% of notional | 31.9 | −13.4 | 1.76 | 23.8 / 1.56 |
| Start ₹3L | 28.8 | −12.5 | 1.80 | 22.5 / 1.60 |
| Start ₹5L | 35.3 | −13.3 | 1.76 | 25.8 / 1.58 |
| Start ₹25L | 41.4 | −13.5 | 1.76 | 28.3 / 1.57 |
| Start ₹50L | 42.2 | −13.6 | 1.76 | 28.5 / 1.57 |
| Half size | 15.6 | −6.5 | 1.85 | 10.9 / 1.63 |

**VIX threshold sensitivity** (`data/reports/realtrade_sensitivity.csv`): all 20 combinations of
short-sleeve VIX ≥ 13–17 and gap-buy VIX ≤ 12–15 are profitable, with 17.7–42% CAGR, −13 to −23%
drawdown and profit factor 1.44–1.76. The chosen point (15/13) is the in-sample best. Its neighbours
(14–16 / 13–14) give 28–40% CAGR, profit factor 1.52–1.76 and drawdown of −15.4% or better. Expect
live results nearer the neighbourhood than the headline.

## 5. Caveats

- **Out-of-sample profit factor is 1.58**, just over the 1.5 target, and slips under it with double slippage. 2026 to date is only +8.7%.
- **The gap buy's VIX ≤ 13 cut-off was chosen in-sample**, and 2023, a low-VIX year, drives much of the IS return. The ≤ 14 and ≤ 15 neighbours give a lower gap-buy profit factor (about 1.45).
- **Margin is a fixed notional percentage, not exchange SPAN.** Check the broker's margin calculator before going live; the 12% case shows the effect.
- **Stops fire on 1-minute closes.** Real ticks can stop earlier or later. A gap through a stop is filled at the next real price.
- **The data ends on 2026-10-01** and covers the nearest weekly expiry only.

## 6. Next steps

1. Paper trade it for at least 8 weeks and compare fills with the replay.
2. Rerun Strategies 1–5 on `research_realtrade.py` to measure how much intrinsic pricing inflated each.
3. Add Strategy 6 to the report and to a bot (the `three_sleeve_bot/` structure fits: three sleeves, a weekly calendar and a VIX gate).

## 7. Crowded-seller measures (2026-10-23, `research_realtrade_crowding.py`)
Seven measures known before the 09:30 entry were tested as "skip when sellers look crowded" filters on S2 and E0. The
crowded third was set on 2021–23 and checked on 2024+. Measures: VRP; recent seller underpayment (actual move vs straddle,
last 8 weeks); today's premium richness (ATM straddle / 0.8·spot·VIX·√T); client, pro and FII net short index options
(z-score, previous day); weekly open interest vs the same day of the last 8 cycles.

- **Only E0 + richness survives.** Skipping the expiry-day strangle when richness is below 1.434 (the 2021–23 bottom
  third) lifts its PF from 1.94 to 2.22 in 2021–23 and from 1.66 to 2.21 in 2024+. PF rises steadily with stricter cuts in
  both periods. Portfolio effect: PF 1.76 → 1.80, 2024+ CAGR unchanged (28.5 → 28.7% at ₹5L), full-period CAGR −1 to −2
  points (fewer 2021 trades), same drawdown.
- **Rejected:**
  - Participant OI (retail/pro/FII crowding): high retail shorting was the best third in 2021–23 and the worst in 2024+,
    with no stable sign.
  - Open interest build-up.
  - VRP and recent underpayment for S2: they help in 2021–23 and fail in 2024+.
- **Caveat:** 14 tests and 1 survivor, with buckets of 15–30 trades. Treat it as a quality filter to watch, not proven.
