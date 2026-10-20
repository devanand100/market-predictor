# Project Handover — Nifty Options Strategy Research (BACKOFF.md)

**Updated:** 2026-09-30 · **Data:** DhanHQ 1-min Nifty weekly options (Jan 2021 – Sep 2026), 1-min spot, 1-min India VIX, NSE participant OI (FII/DII/Client/Pro, 2022–2026)


> **Update 2026-10-01: engine fix.** Strikes outside Dhan's ±500-point window were being forward-filled with stale prices, which understated losses on deep in-the-money shorts. All numbers below are rerun on the fixed engine: Strategy 1 is 12.6% CAGR (was 11.1%) and Strategy 2 is 12.4% CAGR (was 18.0%). Trade-level findings are in `docs/STRATEGY_2_TRADE_ANALYSIS.md`.

## 1. Critical corrections to the earlier work
1. **Options `strike_offset` is rolling ATM-relative.** The old backtests followed "ATM+4" all week, which silently switched contracts. Their results (Iron Condor 20.6% CAGR, Sharpe 2.78) are **invalid**. Those scripts are now in `legacy/`.
2. **Cost model was wrong.** The options exchange charge is ~0.035–0.05% of premium, not 0.00297%. Lot-size history was also wrong. Both are fixed in `cost_model.py`, which is date-aware (STT 0.05% → 0.0625% → 0.1% → 0.15% from Apr 2026).
3. **The expiry calendar is detected from the data** (296 weekly expiries, no gaps). This handles holidays and the Thu → Tue switch in Sep 2025.

## 2. Research results (in-sample 2021–23, out-of-sample 2024–26)
| Idea | In-sample | Out-of-sample | Verdict |
|---|---|---|---|
| Intraday short straddle, non-expiry days | Loses after costs | — | Rejected |
| Expiry-day (0DTE) short straddle | +₹108k, Sharpe ~5 | Lost in 2024, 2025 and 2026 | **Rejected** (edge died after SEBI Nov-2024 changes) |
| FII futures-change directional futures trade | +₹2.19L | **−₹4.01L** | **Rejected**: every FII/DII signal flipped sign OOS |
| Hedged iron condors (wings inside the ±500 data window) | Mostly negative | Mostly negative | Rejected (wings cost more than they save) |
| Intraday ORB option buying (336 configs), 0DTE buying, long straddles/strangles | Some positive | ~0% positive OOS | Rejected (`docs/OPTION_BUYING.md`) |
| **Weekly gap-direction ATM buy, +100% TP, VIX ≤ 15** | +14%/trade, p 0.014 | +24%/trade, p 0.002 | **Strategy 3** (`final_gap_buy.py`) |
| Monthly 45→21 DTE short straddle, VRP ≥ 2 | 18.5% CAGR | 20.0% CAGR | Promising (`docs/MONTHLY_OPTIONS.md`) |
| **Weekly expected-move short strangle** | Positive in all 36 parameter cells | **Positive in all 36 cells**, median Sharpe 1.03 | **Selected** |

## 3. Final strategy (`final_strategy.py`)
- **Entry:** 09:30, exactly 4 trading sessions before expiry. In a normal 5-session week that is the first session after the previous expiry. Weeks with fewer than 5 sessions (holidays) are skipped, which is why there are 216 trades, not about 294.
- **Strikes:** read the ATM straddle premium S. Sell CE at ATM+S and PE at ATM−S, rounded to 50.
- **Stop:** exit both legs if the position loss reaches 1.5× the credit received (checked every minute).
- **Exit:** otherwise close at 15:15 on expiry day. Never hold into settlement.
- **Sizing:** lots of 65. Margin is taken as ~9% of notional; use at most 60% of equity as margin. Compounding.

### Results (₹5,00,000 start, net of all costs and slippage, ₹1,990/month running costs)
| | Full 2021–26 | In-sample 21–23 | **Out-of-sample 24–26** | Nifty buy & hold |
|---|---|---|---|---|
| CAGR | **12.6%** | 6.3% | **20.8%** | 9.6% |
| Max drawdown | −32.9% | −32.9% | −15.5% | −18.5% |
| Sharpe (weekly, annualised) | 0.82 | 0.46 | 1.25 | — |
| Win rate / profit factor | 69.0% / 1.32 | — | 71.3% | — |

Returns by year: 2021 −10.7%, 2022 +10.8%, 2023 +21.2%, 2024 +6.3%, 2025 +35.0%, 2026 YTD +13.5%.

### Running costs over 5.65 years
- Statutory costs: ₹55k.
- Fixed running costs: ₹1.33L (Dhan data API, VPS, misc).
- At ₹5L capital, the fixed costs alone cost about 6.5 CAGR points: 19.1% without them vs 12.6% with them. Capital below ~₹5L is not viable.

### Stress tests
- **Double slippage:** CAGR falls to 7.7%. The edge depends heavily on execution quality.
- **Monte Carlo** (10,000 paths × 5 years, bootstrapped weeks):
  - Median CAGR 17.8%; 5th percentile −1.0%.
  - Median max drawdown −30%; 1-in-20 drawdown −50%.
  - 6.1% chance of losing money over 5 years.
- **Worst week:** −9.0%. Gap risk stays unhedged (no black swan like Mar-2020 exists in the options data).

## 3b. Strategy 2: confirmed directional credit spread (`final_spread_strategy.py`)
Signal research is in `research_directional_spreads.py` and `research_spread_validation.py`. It tested 15 weekly signals (FII/Client/Pro participant OI, PCR, price-action trend, VIX) with credit and debit spreads. Each signal was compared with 10,000 random direction assignments over the same weeks.
- **Rejected:** PCR is no better than random (p 0.6–0.8). Price-action trend was right in only 42–49% of weeks in 2024–26. FII futures signals flipped sign.
- **Kept (corrected engine):** the FII index-options net change (p 0.021 / 0.105) and the opening gap (p 0.198 / 0.014). Trading only when both agree gave p 0.011 / 0.011; bear calls alone p 0.037 / 0.010.
- **Rules:**
  - Both signals agree → at 09:30 on the first session after expiry, sell a credit spread in that direction.
  - Short strike at ATM ∓ 0.5× straddle, long strike 200 points further out.
  - No stop; close at 15:15 on expiry day.
  - Size so that a full-loss week costs at most 8% of equity.
- **Results (May 2022 – Sep 2026, ₹5L, net of everything):**
  - CAGR **12.4%**, max drawdown **−19.1%**, Sharpe 1.46, win rate 84%, 119 trades.
  - 2022–23: 13.8% CAGR. 2024–26: 9.0% CAGR. 2026 YTD: −10.0%.
  - Monte Carlo over 5 years: median 12.5%, 5th percentile 2.9%, 1-in-20 drawdown −27.9%.
  - Bear calls carry it (90% win, +0.94%/trade); bull puts average +0.08%. Bear calls only: 10.9% CAGR, −6.9% DD, Sharpe 2.91.
- **Compared with Strategy 1 over the same period:** the strangle made more (21.8% CAGR, −16.3% drawdown, −8.8% worst week, Sharpe 1.28). Weekly return correlation between the two is 0.16. A 50/50 split gave 15.5% CAGR, −13.7% drawdown and a −6.6% worst week.
- **Caveat:** the two signals were picked after seeing that they held their sign in both periods, so 2024–26 is only semi out-of-sample for this strategy. Forward paper trading is required.

## 4. Honest assessment
The strategy has a genuine, persistent but **thin** edge (the weekly variance risk premium). It beat Nifty buy-and-hold on return (12.6% vs 9.6%) but with a deeper drawdown. Before going live:
1. Paper trade for 8–12 weeks and compare real fills with the modelled slippage.
2. Start at half the size above.
3. Keep F&O business-income tax in mind; the CAGR figures are pre-tax.

## 5. Files
- `build_research_data.py`: consolidated options (by absolute strike), daily features, and the expiry calendar.
- `engine.py`: contract-exact minute engine.
- `cost_model.py`: statutory charges and the slippage model.
- `research_intraday.py`, `research_positional.py`, `research_strangle.py`, `research_signals.py`: the research that led to the table in section 2.
- `final_strategy.py` → `data/reports/final_strategy_summary.json`, `final_trades.csv`.
- Rerun: `python build_research_data.py && python -c "import engine; engine.load_days(rebuild=True)" && python final_strategy.py`
