# Survivor and Wave backtest (2026-11-14)

Raahi Bhushan's open-source algos (`github.com/Raahi-Bhushan/trading-algo`, local copy
`C:\Users\Admin\Documents\trading-algo`) on the real-price replay (`research_realtrade.py`).
Code: `research_survivor.py` (grids: `base`, `baseline`, `exits`, `combo`, `wave`) and
`research_survivor_final.py` (selected config, stresses, accuracy). Outputs: `data/reports/survivor_*`.

## Verdict
- **Survivor works only with a spot stop.** As coded (no exit, hold to expiry) it makes 19.7% CAGR with a
  −43.2% drawdown. Selected on 2021–23: 30-pt trigger, 200 pts OTM, 90-pt reset, 75-pt spot stop, max 6 open
  clips per side → **14.8% CAGR / −8.3% DD / PF 1.37** at ₹10L (IS 15.8% / −4.9% / 1.49, OOS 2024+ 13.7% /
  −8.3% / 1.31). 77% of months positive. All 72 combo cells are positive OOS (median 12.5%, DD −10.5%).
- **Weaker than Strategy 6** (~35% at real margin). Not a candidate for deployment on its own.
- **Cost-sensitive:** slippage ×2 → 9.0% / −14.4%; ×3 → 2.2% / −25.0%. About 3.5 trades a day. 2026 YTD −0.5%.
- **The trend trigger has value:** same strikes sold at 10:00 daily = 7.3% / PF 1.18; mirror trigger
  (sell calls on rises) loses OOS (−10.8%).
- **Wave loses** in 23 of 24 settings (grid keeps shorting into the 2021–26 rally). Rejected.

## Exits (on g30 s200 r30, hold = 20.2% / −43.0%)
| Exit | CAGR | DD |
|---|---|---|
| Spot stop 100 pts | 17.1 | −14.7 |
| Max 4 open clips/side | 18.6 | −19.6 |
| Day loss stop 2% | 18.1 | −26.1 |
| TP 50% of premium | 18.9 | −29.2 |
| Premium stop 1.5× | 16.0 | −21.8 |
| Author's (1.3× stop, book < ₹20 at 50% profit) | 11.1 | −13.2 |
| Expiry day only | 2.6 (OOS −1.9) | −12.5 |
| Intraday only (flat at 15:15) | −3.2 | −39.5 |

Hedged clips (long 100/200 pts further out) lost heavily but are **not judged**: the long legs often leave the
±500 window and are valued at intrinsic (unobserved fills 800–2,600). `survivor_exits.csv` hedge rows also
predate a sizing fix (lots sized on spread margin stacked ~75 lots); ignore them.

## Accuracy (Sensibull "81%")
Accuracy reflects the exit style. Hold losers + book winners at 50% → 90% winning trades, 86% winning
realised-P&L days, average loss ₹10.4k vs average win ₹1.5k, 18.9% / −25.9%. The selected version wins 33.5% of
realised days and 49% of trades but its average loss (₹1.4k) is below its average win (₹2.0k).

## Assumptions / limits
- Nearest weekly expiry only (the trader holds several); strikes within ±450 of ATM; data to 2026-10-01.
- References reset at 09:20 each session (the script keeps state in memory); new sales 09:21–15:00.
- Clip = 4% of equity on 11.6% naked margin (+2% ELM on expiry day from 2024-11-20), ≥1 lot, total margin ≤ 90%.
  At ₹10L each clip is one lot, so the margin cap limits size.
- Wave tested on Nifty spot (1-min close fills) as a proxy for the future; no futures data.
- The run was done in a cloud container without pyarrow: `research_survivor._patch_data()` rebuilds daily
  features from `engine_cache.pkl` with the same expiry rule (299 expiries vs 296 in `daily_features`). On a
  machine with pyarrow the normal `daily_features.parquet` is used.
