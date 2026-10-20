# Strategy 2: Confirmed Directional Credit Spread

Full spec for rebuilding the strategy from scratch. The machine-readable copy is [../strategies/confirmed_spread.json](../strategies/confirmed_spread.json). The code is the source of truth:
- [final_spread_strategy.py](../final_spread_strategy.py)
- [research_directional_spreads.py](../research_directional_spreads.py) (`signal_table`, `make_legs_fn`)
- [research_positional.py](../research_positional.py) (`run_cycle`, `cycles`)
- [cost_model.py](../cost_model.py)

> **2026-10-20:** with participant OI back to 2021 the backtest starts May 2021: 144 trades, 6.5% CAGR, −18.6% DD; 2021 lost 8.4%. The signal still works from May 2022 (12.2%) but failed on the newly testable 2021–Apr 2022 weeks. Numbers further down predate this.

## 1. Rules
| Step | Rule |
|---|---|
| Universe | NIFTY weekly options, nearest expiry |
| Entry time | 09:30 IST, first session after each weekly expiry |
| Signal A: FII options | From the NSE participant OI file of the session *before* entry: `x = (FII CE long − CE short) − (FII PE long − PE short)` for index options. `raw = x − x(previous session)`. `A = raw − median(raw over all past entry weeks)`, needing at least 20 past weeks. Bullish if > 0. |
| Signal B: gap | `spot(09:30) / previous close − 1`. Bullish if > 0. |
| Filter | Trade only if sign(A) == sign(B). Otherwise no trade (about half the weeks) |
| Strikes | `S` = ATM CE + ATM PE at 09:30 (ATM = spot rounded to 50). `d = max(50, round(0.5·S/50)·50)` |
| Bullish | Sell PE at ATM−d, buy PE at ATM−d−200 |
| Bearish | Sell CE at ATM+d, buy CE at ATM+d+200 |
| Stop / target | None. Loss is capped by the spread |
| Exit | 15:15 on expiry day. Never held to settlement, so no exercise STT |
| Size | `lots = max(1, floor(0.08 · equity / (200 · 65)))`, compounding, lot = 65 |

## 2. Data used
| Data | Location | Used for |
|---|---|---|
| Nifty weekly options, 1-min (Dhan) | `C:\Users\Admin\Documents\Trading Bot\minute_parquet\options\year=*/month=*/options.parquet` | Leg prices and the straddle `S` |
| Nifty spot, 1-min | `...\minute_parquet\nifty_spot_1min.parquet` | Gap, ATM, daily closes |
| India VIX, 1-min | `...\minute_parquet\india_vix_1min.parquet` | Not used by Strategy 2 |
| NSE participant OI (daily) | `data/processed/participant_oi_features.csv` (from `nse_data_downloader.py`) | Signal A |

**Data traps** (all handled in code; read before writing a new backtest):
1. The Dhan options `strike_offset` / `strike_label` columns are **rolling ATM-relative**: the `strike` behind "ATM+4" changes every time spot crosses 50 points. Always key legs by the absolute `strike`. `engine.Day.px(strike, type, minute)` does this.
2. The options file holds only the **nearest weekly expiry** and only **±10 strikes (±500 points) around the current ATM**. A strike outside that window has no price, so the engine uses intrinsic value. Never forward-fill across the window edge: that froze deep in-the-money shorts at stale prices and overstated Strategy 2 (18.0% → 12.4% CAGR once fixed). Keep every leg inside ±500 at entry.
3. Expiry dates are **detected from the data** (`build_research_data.detect_expiries`): the ATM straddle collapses, then resets the next morning. This covers holidays and the Thu → Tue switch in Sep 2025. Do not hard-code weekdays.
4. The NSE participant OI file is published after the close, so it is lagged by one session (`prev_*` columns in `daily_features.parquet`). Using same-day values is look-ahead bias.

## 3. Fill and cost model
- Fills at the 1-minute close. A sell fills at `close − slip` and a buy at `close + slip`, where `slip = 0.3 + 0.5% × premium` points.
- Charges come from `cost_model.option_fill_cost` and depend on the date:
  - brokerage ₹20 per order;
  - STT on sells: 0.05%, then 0.0625% from Apr-2023, 0.1% from Oct-2024, 0.15% from Apr-2026;
  - NSE transaction charge: 0.0503%, then 0.03503% from Oct-2024;
  - SEBI fee ₹10 per crore; stamp duty 0.003% on buys; GST 18%.
- Fixed running costs are ₹1,990/month (`cost_model.MONTHLY_RUNNING_COSTS`).
- In the backtest, per-lot statutory costs are multiplied by the number of lots, which slightly overstates brokerage (conservative).

## 4. Expected results (use these to check a rebuild)
₹5,00,000 start, 27 May 2022 – 1 Sep 2026, net of everything. Corrected engine (2026-10-01).

| Metric | Value |
|---|---|
| Trades | 119 |
| Final equity | ₹8,24,583 |
| CAGR (full / 2022–23 / 2024–26) | 12.4% / 13.8% / 9.0% |
| Max drawdown | −19.1% (Dec 2025 – Apr 2026) |
| Sharpe (weekly, annualised) | 1.46 |
| Win rate | 84.0% |
| By year | 2022 +10.2%, 2023 +10.9%, 2024 +13.3%, 2025 +32.5%, 2026 YTD −10.0% |
| Monte Carlo, 5 years | median 12.5%, 5th pct 2.9%, 1-in-20 DD −27.9% |
| Bear calls only (variant) | 10.9% CAGR, −6.9% DD, Sharpe 2.91 |

If a rebuild gives a different trade count or final equity, one of these has changed: the input data, the expiry detection, the signal lag or centring, the engine's out-of-window handling, or the cost model. Trade-level analysis: [STRATEGY_2_TRADE_ANALYSIS.md](STRATEGY_2_TRADE_ANALYSIS.md).

## 5. Why this rule, and how much to trust it
- 15 weekly signals were tested with credit and debit spreads: FII/Client/Pro OI, PCR, price-action trend, VIX. Each was compared with 10,000 random direction assignments (`research_spread_validation.py`).
- PCR and trend signals were no better than random. The FII futures signal flipped sign after 2023.
- Corrected engine: FII options change p = 0.021 (2022–23) and 0.105 (2024–26). Gap: p = 0.198 and 0.014. Both agreeing: p = 0.011 and 0.011; bear calls only: p = 0.037 and 0.010.
- **Caveat:** the two signals were chosen after seeing that they held their sign in both periods, so 2024–26 is only semi out-of-sample here. Paper trade for 8–12 weeks before using real money.

## 6. Rebuild from scratch
```bash
pip install -r requirements.txt
# 1. NSE participant OI (skip if data/processed/participant_oi_features.csv exists)
python nse_data_downloader.py --start 2022-01-01 --features
# 2. Options/spot/VIX -> data/processed/options_1min.parquet + daily_features.parquet (~1.5 min)
python build_research_data.py
# 3. Engine cache (~20 s). Build it via import, NOT `python engine.py` (that pickles a __main__.Day class)
python -c "import engine; engine.load_days(rebuild=True)"
# 4. Strategy 1 (also used for the portfolio comparison), then Strategy 2
python final_strategy.py
python final_spread_strategy.py   # -> data/reports/spread_strategy_summary.json, spread_strategy_trades.csv
```

## 7. Live execution checklist
1. Evening before entry (expiry day): download that day's NSE participant OI and compute Signal A.
2. At 09:30 on entry day: compute Signal B and the ATM straddle `S`. If A and B disagree, do nothing this week.
3. Place the long (hedge) leg first, then the short leg, so margin relief applies.
4. Hold the spread; close both legs at 15:15 on expiry day.
5. Log the actual fills and compare them with the modelled slippage.
