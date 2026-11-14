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
3. Add Strategy 6 to the report and to a bot (done 2026-10-22: `strategy6_bot/`, built from the former `three_sleeve_bot/`).

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

## 8. Kill switch (2026-10-23, `research_killswitch.py`)
Eight rules were tested on the shadow record (the full strategy always trades; a rule decides from closed trades,
live equity or VRP before each entry). Shadows used: real 2021–26, synthetic COVID 2019-11..2020-12 chained to real,
and the harsh synthetic case with IV ×0.85 above VIX 21.

| Rule | Real 2021–26 | COVID base | COVID harsh |
|---|---|---|---|
| none | 38.8% / −11.6% | 39.8% / −22.0% | 31.9% / −32.5% |
| **drawdown −15%, resume at 20-trade PF ≥ 1.3** | never fires | 36.5% / −19.2% | 31.4% / −19.0% |
| drawdown −20% | never fires | 35.1% / −24.7% | 33.4% / −21.1% |
| 20-trade PF < 0.8 | 37.6% / −12.9% | 36.8% / −25.8% | 32.0% / −23.8% |
| 40-trade PF < 0.9 / sleeve 15-trade PF < 0.5 | never fire | never fire | worse |
| VRP 20-day < 0 (strangles) | 31.0% / −17.6% | 31.4% / −25.4% | 26.1% / −30.9% |

Each cell is CAGR / max drawdown on the trade-close curve. The drawdown −15% rule is the only one that never fires on
real data and caps both COVID drawdowns near −19%, at a cost of 0.5–3.3 CAGR points in those years. Profit-factor rules
trigger on normal bad patches or not at all. The bot reports it **notify only**: paper keeps trading.

## 9. Sizing at Dhan's real margin (2026-10-23, `research_realtrade_real_margin.py`)
The results above size every strangle on the 9% margin model. Dhan's calculator asks ~11.6% of notional
(₹1.70–1.72L per lot at Nifty ~22,700, lot 65), plus 2% ELM on expiry day. The paper bot sizes lots on the model and
cuts them only when Dhan's figure breaks the 90% cap, so at ₹5L S2 takes 2 lots (₹3.40L at Dhan) and E0 can find no
room on expiry day (first seen on the 2026-10-23 entry). The replay was rerun with Dhan-level margin booked:

| Sizing (₹5L, weights 0.55 / 0.55 / 0.05) | CAGR | Max DD | Trade PF | 2024+ CAGR | E0 trades |
|---|---|---|---|---|---|
| 9% model (the backtest above) | 38.6% | −13.3% | 1.76 | 27.6% | 135 |
| **bot: model lots, cut at the 90% cap on Dhan margin** | **34.8%** | **−13.3%** | 1.77 | **25.6%** | 134 |
| lots sized on Dhan margin (weight × equity ÷ Dhan per lot, min 1) | 31.0% | −13.2% | 1.76 | 23.7% | 135 |

Sizing on Dhan's margin halves S2 in most weeks and gives up more than the rare skipped E0 is worth, at no drawdown
benefit, so it was rejected; the bot keeps model sizing. The research figure at ₹10L drops from 38.7% to 35.5%
(0.45 / 0.45 weights). Capital sweep with the bot's weights and sizing:

| Capital | ₹3L | ₹4L | ₹5L | ₹7.5L | ₹10L | ₹15L | ₹20L |
|---|---|---|---|---|---|---|---|
| CAGR / max DD | 28.7% / −14.7% | 31.1% / −13.1% | 34.8% / −13.3% | 36.2% / −13.4% | 38.0% / −13.4% | 39.3% / −13.5% | 39.7% / −13.6% |
| 2024+ CAGR | 23.9% | 25.6% | 25.6% | 26.8% | 27.3% | 27.5% | 28.0% |
| E0 trades skipped | 22 | 27 | 1 | 0 | 0 | 0 | 0 |

₹5L is where the expiry-day crowd-out stops mattering; above it more capital adds 1–2 points a year (finer lots).
Expect about 25% a year at ₹5L on recent data, not the 38.7% headline.

## 10. Live margin check, capital and GAP tweaks (2026-10-27)
**Expiry-day margin is higher than modelled.** On 2026-10-27 Dhan asked ₹7.55L for 3 E0 lots in the Strategy 7 book
(₹2.52L per lot, ~17% of notional at Nifty 22,740) against the ~13.6% (11.6% + 2% ELM) used in §9. S2 lots stayed at
~₹1.70L (11.6%). With the bot's sizing (model lots, cut at the 90% cap on Dhan margin), the arithmetic at today's prices:

| Capital | S2 lots | E0 while S2 is open on expiry day |
|---|---|---|
| ₹5L | 2 (₹3.40L) | skipped (needs ₹5.92L, cap ₹4.50L); happened on 2026-10-27 |
| ₹6.6–7.2L | 2 | 1 lot fits |
| ₹7.3–8.4L | 3 (₹5.10L) | skipped again |
| ₹8.5L+ | 3 | fits |

The replay barely moves with 17% (₹5L 34.8%, ₹7L 36.5%, ₹10L 37.8%; 2024+ 25.7 / 26.6 / 27.0%) because older years
traded at lower Nifty with no expiry-day ELM before 2024-11-20; at today's index level the clash happens every week S2
is open on expiry day. Best capital now: ₹7L (**superseded by §11**: a replay that starts at ₹7L in 2024 or 2025
grows into the 3-lot band and skips E0 about as often as ₹5L; only ₹10L+ avoids it). Floors: S2 needs ₹2.42L, E0 ₹2.96L (both on the 0.55 weight and
the model margin); ₹3L is the absolute minimum (E0 stops above Nifty ~23,077), **₹3.5L the practical minimum** (still
trades E0 after a −13% drawdown). At ₹4L: 30.6% / −13.3%, 2024+ 24.5%. Strategy 7 needs ₹10L+ (MON and E0 never size
below that).

**GAP sleeve tweaks (none adopted).** One change at a time, GAP alone at ₹10L, picked on 2021–23, checked on 2024+
(base PF 2.37 / 1.56, 73 trades): direction from 5/20-day trend instead of the gap loses (0.51/0.74, 0.99/0.59), so the
gap is the edge; entry 2–3 days before expiry loses; target +75% is best in-sample (3.81) but no better OOS (1.58);
target +150/200% worse OOS with −26% DD; exit 1–2 days early about equal; stop −50% hurts IS (1.55), helps OOS (1.88);
VRP ≤ 2 filter unstable (OOS 2.97 at ≤ 2, 1.24 at ≤ 2.5; 23 trades). Skipping gaps < 0.05–0.15% improved both periods
on GAP alone, but in full Strategy 6 it lowered CAGR (₹4L 30.6 → 28.8–30.4%) and deepened drawdown (−13.3 → −15.4 to
−16.4%), so rejected. GAP by year at ₹4L (net, % of account at year start): 2021 −₹13.5k (−3.4%), 2022 none, 2023
+₹3.94L (+68.9%, VIX 10.5–13 all year, 28 trades), 2024 +₹2.22L (+21.9%), 2025 +₹2.43L (+17.4%), 2026 to Sep
+₹1.19L (+7.0%); return on premium falling 39% → 16% → 10%.

## 11. Paper record and the expiry-day crowd-out (2026-11-14, `research_realtrade_expiry_margin.py`)
**First 3.5 weeks of paper** (`strategy6_bot`, both books started 2026-10-22; marked at the 2026-11-13 close, open S2
included). Nifty rose ~11% (22,600 → 25,300) with VIX 15–18, the worst tape for short strangles.

| Book | Start | Mark | Return | Closed trades net | Worst DD (mark) | Closed won / lost |
|---|---|---|---|---|---|---|
| Strategy 6 | ₹5L | ₹4,81,730 | −3.7% | −₹18,781 (+₹1,180 data fees) | −7.1% (11-06) | 2 / 2 |
| Strategy 7 | ₹15L | ₹14,90,916 | −0.6% | −₹12,978 (+₹1,180 data fees) | −4.1% (11-06) | 3 / 3 |

- The 2026-10-30 S2 (2 lots) was stopped at the first tick of expiry day 2026-11-03 after a ~370-point gap up: stop level
  130.34, first available exit 274.45, −₹27,332 (−5.4% of the book). The rules worked as written; the size is the gap.
- Strategy 6 skipped E0 on 2026-10-27 and 2026-11-03 (2-lot S2 still open, no room at Dhan margin); Strategy 7 took both
  (+₹16,292, −₹1,342) and that is most of the gap between the books. GAP never armed (VIX > 13). Strategy 7's monthly
  straddle has had no entry window yet (first one: the 2026-12-29 expiry, from 2026-11-16, needs VRP ≥ 2).
- Entry spreads are 0.2× the modelled slippage; no health alert, kill switch far away (−15% / −16%).

**Why paper skips E0 far more than the replay said.** §9 and §10 quote "1 skip in 135 weeks at ₹5L", but that replay
starts with ₹5L in Jan 2021 and has compounded to several times that by 2024, so margin stops binding exactly in the
high-Nifty years. Starting the replay later, at the bot's sizing (model lots cut to 90% of equity at Dhan margin),
weights 0.55 / 0.55 / 0.05, with expiry-day entries at Dhan's ~17% (data to 2026-10-01):

| Start | Capital | E0 trades: 9% model | bot, expiry 13.6% | bot, expiry 17% | CAGR model / bot 17% | DD bot 17% |
|---|---|---|---|---|---|---|
| 2024-01 | ₹5L | 43 | 28 | 28 | 23.4% / 20.5% | −12.0% |
| 2024-01 | ₹7L | 43 | 43 | 29 | 24.8% / 21.7% | −12.6% |
| 2024-01 | ₹10L | 43 | 43 | 43 | 26.5% / 23.2% | −13.3% |
| 2025-01 | ₹5L | 27 | 21 | 12 | 13.8% / 14.0% | −11.7% |
| 2025-01 | ₹7L | 27 | 22 | 12 | 17.1% / 16.8% | −12.2% |
| 2025-01 | ₹10L | 27 | 27 | 27 | 17.4% / 16.2% | −13.1% |

So an account that holds ₹5–7L at today's index level loses about half its E0 trades, which matches paper (2 of 3).
The cost in return is small, 0–3 points a year (from 2025 skipping E0 even helped at ₹5L), because the E0 skipped is
the one stacked on an open S2. ₹7L is not a fix: the account grows into the 3-lot S2 band (₹7.3–8.4L) and is crowded out
again. ₹10L trades every E0; its lower CAGR against the model comes from S2 lots cut at Dhan margin, not from E0.
Decision: keep ₹5L on paper and compare paper with these 2024+/2025+ figures, not the 2021-start 34.8%.

Not yet done: replaying the paper weeks themselves trade by trade. The engine cache ends 2026-10-01; it needs
`update_market_data.py` with a valid `DHAN_ACCESS_TOKEN` in `.env` (the `.env` token goes stale whenever its owner renews).

**E0 on its own** (`research_realtrade_e0_only.py`, 2021 to 2026-10-01, weight 0.55, running costs ₹1,990/month on):

| | ₹5L 9% model | ₹5L Dhan 17% | ₹10L Dhan 17% | Strategy 6 ₹5L Dhan 17% |
|---|---|---|---|---|
| CAGR / max DD | 6.6% / −7.8% | 4.7% / −6.4% | 7.8% / −6.5% | 32.8% / −13.1% |
| 2021–23 / 2024+ CAGR | 10.6 / 2.5% | 8.4 / 0.9% | 11.6 / 3.7% | 39.8 / 25.6% |
| By year 2021…2026 | | +10.3, +16.1, −0.7, +5.8, −0.2, −2.9 | +12.3, +21.4, +1.8, +9.7, +2.5, −1.6 | +13.6, +33.7, +79.6, +37.7, +25.0, +8.8 |

E0 trades 135 times (37 / 48 / 7 / 16 / 13 / 14 a year), PF ~1.9 overall but win rate down from ~60% (2021, 2024) to
46% (2025) and 43% (2026); it is near break-even since 2025. It sits idle most days, so running costs weigh on it (~4.8
points a year at ₹5L). The crowd-out above therefore costs little on recent data. Dropping E0 altogether is untested
inside the full strategy (it trades on days S2 is idle or stopped).
