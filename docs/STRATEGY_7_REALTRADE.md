# Strategy 7: Strategy 6 + monthly straddle, for ₹10L+ accounts (real-market replay)

Status 2026-10-22: research done, **not deployed**. Code: `final_realtrade_s7.py` (rules and stress tests);
research: `research_realtrade_s7.py` (stage `l2` and stage `port`). This is the improved successor of
Strategy 5 / `three_sleeve_bot`, tested on the real-price replay (`research_realtrade.py`).

## What changed from Strategy 5 (three_sleeve_bot)
| Strategy 5 sleeve | Real-price result | Strategy 7 |
|---|---|---|
| L2 staggered strangle, every week | PF 1.27 per week; 1.46 when gated to VIX ≥ 15, but tranches stay ~1.25 and it trades the same weeks as S2 | **Dropped** |
| Gap buy, VIX(09:20) ≤ 15, 3% | PF 1.56 | Gap buy at previous-close VIX ≤ 13, 5% (Strategy 6) |
| Monthly straddle, VRP ≥ 2, 20% | PF 2.2 even with real-only pricing | **Kept**, 20% (weight not re-tuned) |
| – | – | **Added** the S2 and E0 strangles of Strategy 6 (VIX ≥ 15) |

The VIX 13–15 band loses for every short-strangle variant (L2 there: PF 0.98), so nothing trades in it.

## Rules
Strategy 6 (`docs/STRATEGY_6_REALTRADE.md`) at 45% / 45% / 5%, plus MON: when the previous session's VIX − RV20 ≥ 2,
sell the ATM straddle of the monthly expiry at about 45 DTE (~10:15), buy it back at 21 DTE, no stop, 20% of equity as
margin. Margin is 11.6% of notional (+2% on expiry day), from Dhan's quote on 2026-10-22, capped at 90% of equity.
Running costs ₹590/month.

MON P&L uses real prices only (`research_monthly_realonly.py`). 12 of its 40 trades exit at a strike with no traded
price. Those use the last real price moved by the change in intrinsic value, plus urgent slippage, which is conservative.

## Results (₹15L, Jan 2021 – Sep 2026)
| | CAGR | Max DD | Trade PF | 2021–23 | 2024+ |
|---|---|---|---|---|---|
| Strategy 7 | **39.1%** | **−15.0%** | **1.80** | 47.7% / −14.5% / 2.28 | **30.2% / −15.0% / 1.65** |
| Strategy 6 alone | 35.3% | −13.4% | 1.76 | 45.0% / −13.4% / 2.35 | 25.3% / −13.4% / 1.57 |
| Strategy 5 as the bot ran it | 18.6% | −15.8% | 1.48 | 8.9% / −15.8% / 1.28 | 30.1% / −15.8% / 1.56 |

Years: 2021 +23.4%, 2022 +43.8%, 2023 +81.3%, 2024 +50.0%, 2025 +24.3%. Per-sleeve PF: S2 1.90, E0 1.97, GAP 1.70,
MON 2.24.

## Stress tests (`final_realtrade_s7.py`)
| Case | CAGR | Max DD | PF | 2024+ CAGR / PF |
|---|---|---|---|---|
| Slippage × 2 | 33.8 | −14.9 | 1.68 | 24.6 / 1.52 |
| Margin 14% | 35.1 | −14.2 | 1.80 | 27.5 / 1.63 |
| MON weight 0.15 | 38.1 | −13.9 | 1.80 | 29.1 / 1.63 |
| MON weight 0.25 | 40.5 | −16.0 | 1.82 | 31.5 / 1.67 |
| Start ₹10L | 37.7 | −14.7 | 1.81 | 30.0 / 1.66 |
| Start ₹25L | 40.6 | −15.0 | 1.81 | 30.7 / 1.66 |
| Start ₹50L | 40.8 | −15.1 | 1.81 | 30.8 / 1.65 |

## Caveats
- **MON has no stop.** Its worst trade lost ₹3.9L on the ₹15L run, by then on a larger account. It is the main tail risk, and a gap like March 2020 is not in the data.
- **MON is simulated differently** from the other sleeves. It runs on 60-minute monthly data, marked daily from its own MTM path, not minute by minute.
- **Minimum capital is about ₹10L.** One MON lot needs about ₹1.72L margin, which is 20% of about ₹8.6L.
- **Expect less live.** As with Strategy 6, expect live results below the headline: the rules were chosen on 2021–23, and execution will be worse than modelled.

## Kill switch (2026-10-23)
The bot's notify-only kill switch fires at **−16%** for this book, against −15% for Strategy 6. Strategy 7's real
2021–26 daily mark-to-market worst is −14.97% (29 Sep 2026), so −15% has no headroom. On the COVID synthetic tests of
its weekly sleeves (the same as Strategy 6 at 0.45 / 0.45 / 0.05), −16% keeps the drawdown at −19% (base 36.8% CAGR,
harsh 33.5%). −17% and −18% let the base case reach −21.7%, and −20% reaches −24.7%. Restart is unchanged: last
20 trades PF ≥ 1.3.

**Confirmed on the full Strategy 7 2020 test** (`research_synthetic_s7.py`, monthly straddle included, chained to real
2021–26, restart at 20-trade PF ≥ 1.3):

| Trigger | COVID base | COVID harsh (IV ×0.85) |
|---|---|---|
| none | 39.5% / −17.0% | 32.3% / −28.2% |
| −15% or −16% | 38.5% / −17.0% | 34.3% / −16.1% |
| −17% | 39.5% / −17.0% | 33.9% / −17.7% |
| −18% | 39.5% / −17.0% | 33.4% / −19.7% |
| −20% | 39.5% / −17.0% | 31.3% / −22.3% |

Each cell is CAGR / max drawdown on the trade-close curve. On real 2021–26 data no trigger fires. −16% matches −15%
in both crash cases and keeps 1 point of headroom over the real worst of −14.97%.

## To deploy later
Add a MON sleeve to `strategy6_bot`. `three_sleeve_bot/lambda_function.py` already has `MONTHLY_1015` (VRP from
Dhan daily closes, monthly expiry chain, strike nearest the forward) and the rules in `three_sleeve_bot/rules.py`
(`mon_entry_due`, `mon_exit_due`, `mon_strike`). Add the Dhan margin check before the MON entry, a parity test against
`research_realtrade_s7.MonthlyReal`, and set the capital to ₹10L or more.
