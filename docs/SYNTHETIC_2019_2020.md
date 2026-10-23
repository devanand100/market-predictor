# Strategy 6 on synthetic 2019-11 .. 2020-12 option prices (COVID stress test)

Script: `research_synthetic_options.py` (`calibrate`, `check`, `stress`). Outputs in `data/reports/synthetic_*`.
Date: 2026-10-22.

## Why
Real Dhan option prices start 2021-01. Nifty 1-min spot starts 2019-11-18 and the daily India VIX close
starts 2019-06, so 2019-11 .. 2020-12 (VIX median 21, peak 83.6 on 2020-03-24) can only be tested on
model prices. This window holds the 2020 crash, which no real-price backtest covers.

## Pricing model (fitted on real 2021-2023 prices)
- Black-76, T = (sessions left + fraction of today) / 252, expiry 15:30. Carry r = 2.65%/yr from ATM parity.
- Minute VIX is modelled because pre-2021 VIX is daily only: it moves with spot intraday at
  dlnVIX = -3.89 x dlnSpot and ends at the real VIX close. Median error against the real minute VIX
  (2021-08 onward) is 1.0%, p90 is 3.1%. Strategies still decide on the previous session's VIX close.
- Correction surface: IV/VIX by sessions to expiry (0-4) x 7 time-of-day buckets x 5 VIX buckets x
  moneyness z = ln(K/F)/(v sqrt T). Plain BS(VIX) is wrong by a lot: weekly ATM IV is 0.64-0.95 x VIX,
  and the put skew reaches 1.25 x at z = -3.
- Expiries before 2021: Thursday, or the previous session if Thursday is a holiday. On 2021-01 .. 2025-08 this
  rule matches 242 of 243 real expiries; the miss is the Muhurat session.

## Validation against real prices
| | median synthetic/real, dte 1-4 | dte 0 OTM | median abs error |
|---|---|---|---|
| 2021-23 (fit) | 0.99-1.01 | 0.75-0.96 (too cheap) | 16% |
| 2024+ (check) | 0.67-0.97 (too cheap) | 0.81-1.04 | 22% |

Strategy 6 on 2021-26, same rules and sessions, at Rs 10L:

| | CAGR | DD | PF | S2 PF | E0 PF | GAP PF |
|---|---|---|---|---|---|---|
| real prices | 38.7 | -13.5 | 1.76 | 1.90 | 1.85 | 1.70 |
| synthetic | 33.8 | -21.0 | 1.54 | 1.33 | 1.46 | 1.64 |

Per-trade P&L correlation with the real trades: S2 0.62, E0 0.66, GAP 0.93. The synthetic data makes
the short sleeves look worse than they really were, so the 2020 numbers below lean pessimistic.

## 2019-11-18 .. 2020-12-31, Strategy 6 at Rs 10L
VIX was >= 15 almost all year, so the short strangles traded nearly every week (47 S2, 49 E0, 2 GAP).

| case | CAGR | max DD | PF | 2020 |
|---|---|---|---|---|
| base | 45.2 | -22.0 (2020-01-30 to 04-07, recovered 08-20) | 1.78 | +41.5 |
| slippage x2 | 29.9 | -23.0 | 1.50 | +25.2 |
| slippage x3 | 19.2 | -25.4 | 1.34 | +13.8 |
| margin 12% / 15% | 31.8 / 24.5 | -15.8 / -13.0 | 1.76 / 1.78 | +27.8 / +20.5 |
| half size | 17.8 | -10.2 | 1.76 | +16.8 |
| IV trend above VIX 21 | 85.9 | -11.2 | 2.46 | +86.6 |
| IV x0.85 when VIX > 21 | 1.6 | -31.7 | 1.06 | -5.0 |
| slippage x3 + margin 15% | 15.0 | -14.6 | 1.47 | +10.3 |
| IV x0.85 + slippage x3 + margin 15% | -3.3 | -22.7 | 0.98 | -9.0 |

Chained (synthetic 2019-20, then real 2021+): 41.0% CAGR, -22.0% DD; every calendar year positive.

Worst phase: Feb-Jun 2020. March 2020 alone was -14.4%. The worst trade was the E0 of 2020-03-19:
-Rs 81k on Rs 92k of credit, so the 0.5x stop filled far past its level. Five of the ten worst trades
come from the March-May 2020 high-VIX weeks.

## How far to trust it
- **The result depends most on the weekly IV/VIX ratio above VIX 30, which we cannot calibrate.**
  The surface holds the ratio flat at the VIX 21+ level (~0.89). On real 2021-26 data the ATM ratio rises
  with VIX (0.89 at 21-24, 0.93 at 24-27, 1.06 at 27-35 over 12 days), so the base case probably
  understates the 2020 premiums. The "IV trend" case extrapolates that rise (VIX^0.15).
  The x0.85 case runs against that evidence, but it shows how thin the edge is if crisis weeklies were cheap.
- Spreads in March 2020 were far wider than our slippage model assumes, and SPAN margins rose sharply.
  Use the slippage x3 + margin 15% rows as the realistic floor: ~15% CAGR, ~-15% DD.
- Spot paths and VIX levels are real; only option prices are modelled. Treat the result as "survives
  2020, worst DD about -22 to -32%", not as a CAGR to compare with the real-price backtests.

## Strategy 7 (Strategy 6 + monthly straddle) on 2019-11 .. 2020-12
Script: `research_synthetic_s7.py` (`calibrate`, `check`, `stress`). Same weekly synthetic days, plus synthetic
MON trades from a separate monthly IV/VIX surface (calendar DTE x VIX x z) fitted on the real 2021-23 hourly
monthly chains. ATM monthly IV is 0.85-0.96 x VIX. Settings as `final_realtrade_s7.py`: Rs 15L, margin 11.6%,
MON 20% at VRP >= 2, bot running costs.

**MON check on 2021-26 (same 68 expiries, 100% same entry dates).** The synthetic credit is 0.95-0.98 of real.
Where the real exit had traded prices for both legs, the exit cost matches (median 0.996). Strategy 7's
real-only MON prices a leg with no quote (strike > ~450 pts from spot at exit, 25 of 68 trades) at its last
price moved by intrinsic, a median 1.23 x fair value. VRP >= 2 trades, Rs per lot:

| fair synthetic | research (parity) | real-only (S7) | synthetic + 1.234 stale haircut (used) |
|---|---|---|---|
| 256,740 | 298,911 | 191,611 | 90,480 |

The haircut version is the more pessimistic one. S7 on 2021-26 gives 33.4% / -16.0% / PF 1.57 synthetic,
against 39.1% / -15.0% / 1.80 real.

**Results (Rs 15L)**

| case | CAGR | max DD | PF | 2020 |
|---|---|---|---|---|
| base (MON with stale haircut) | 38.4 | -20.9 (2020-01-30 to 03-24, recovered 05-21) | 1.67 | +34.2 |
| without MON (S6 at 15L, 11.6% margin) | 37.3 | -17.0 | 1.78 | +33.1 |
| MON fair exits | 52.7 | -20.9 | 2.00 | +49.9 |
| slippage x2 / x3 | 25.9 / 19.7 | -18.6 / -19.5 | 1.44 / 1.33 | +20.9 / +14.3 |
| margin 15% | 31.1 | -16.1 | 1.72 | +26.8 |
| start 10L | 34.5 | -18.1 | 1.62 | +30.3 |
| IV trend above VIX 21 | 69.6 | -20.1 | 2.23 | +68.5 |
| IV x0.85 when VIX > 21 | 0.1 | -26.0 | 1.01 | -6.5 |
| slippage x3 + margin 15% | 17.9 | -17.4 | 1.39 | +12.8 |
| IV x0.85 + slippage x3 + margin 15% | -2.8 | -22.7 | 0.95 | -9.1 |

Chained (synthetic 2019-20, then real 2021+): 39.6% CAGR, -20.9% DD, PF 1.80; every calendar year positive.

MON in 2020: the VRP filter skipped the Feb-10 entry (VRP -2), which would have lost Rs 38.8k per lot in the crash.
It entered on 2020-03-16 at a previous-session VIX of 51 (+Rs 27k per lot). Its 7 trades had PF 1.15. The losses came after the crash
(Jun-Jul and Nov-Dec 2020 rallies: worst -Rs 86k on 3 lots). Monthly returns: Mar 2020 -9.6%, Jun 2020 -5.7%;
all other months positive. In 2020 MON adds ~1 CAGR point and ~4 DD points over Strategy 6 at the same capital,
so the crash does not make the case for it; its case rests on 2021-26.
