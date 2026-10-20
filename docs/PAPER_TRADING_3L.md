# Paper-trading checklist: Rs 3L, 50/50 gap buy + credit spread

Backtest (`research_buy_spread_combo.py`, Jun 2022 – Sep 2026, manual, net of charges):
**19.3% CAGR, −9.3% max DD, worst week −3.5%.** By year: 2022 +3.4, 2023 +21.5, 2024 +24.4,
2025 +24.8, 2026 YTD +10.3. Paper trade at least 8–12 weeks and compare with the backtest
before using money.

Capital: Rs 1.5L sleeve A (gap buy), Rs 1.5L sleeve B (credit spread). Each sleeve compounds
on its own; rebalance to 50/50 once a quarter.

## Which weeks
Only weeks with 5 trading sessions to expiry (expiry is detected from the NSE calendar, Tuesday
since Sep 2025). Entry day = the first session after the previous expiry. Skip holiday weeks and
special sessions (e.g. Muhurat).

## Evening before entry (expiry day)
- [ ] Download the NSE participant-wise OI file for today and compute Signal A (FII index
      options change, `docs/STRATEGY_2_CONFIRMED_SPREAD.md` §1).

## Entry day, 09:30
- [ ] Gap = Nifty spot at 09:30 / previous close − 1. Up or down?
- [ ] India VIX at 09:20.

**Sleeve A, gap buy** (only if VIX ≤ 15):
- [ ] Gap up → buy ATM CE, gap down → buy ATM PE, nearest weekly expiry (ATM = spot rounded to 50).
- [ ] Lots = 5% of sleeve A ÷ (premium × 65), rounded down; if that is 0 but one lot costs
      ≤ 10% of the sleeve, take 1 lot.
- [ ] Immediately place a limit/GTT sell at 2× your fill price. **No stop-loss.**
- [ ] If the target has not filled, sell at 15:15 on expiry day. Never hold to settlement.

**Sleeve B, credit spread v2** (only if Signal A and the gap agree):
- [ ] Follow `docs/STRATEGY_2_CONFIRMED_SPREAD.md` §1 for strikes (short ATM ∓ 0.5× straddle,
      long 200 points further). Buy the hedge leg first.
- [ ] Skip if credit < 23.2% of max loss, i.e. credit/(200 − credit) < 0.232 (credit < ~38 pts).
- [ ] Risk: bear call 8% of sleeve B, bull put 4%; lots = risk ÷ (200 × 65), at least 1.
- [ ] No stop; close at 15:15 on expiry day.

## Log every trade
Date, VIX, gap, signal A, strikes, fill prices, exit prices, charges, P&L. Compare your actual
fills with the 09:30 / 15:15 prices: the backtest assumes slippage of 0.3 pt + 0.5% of premium.
