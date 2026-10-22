# strategy6_bot: Strategy 6 paper trader (₹5L)

Paper-trades Strategy 6 (`../docs/STRATEGY_6_REALTRADE.md`) live on Dhan market data. **No orders are placed.**
It runs on AWS Lambda with EventBridge Scheduler, journals everything to MongoDB (database `strategy6`) and
posts **one Discord message per run** to `#strategy6-paper`. It is fully separate from `three_sleeve_bot/`:
own function, schedules, alarms, database, channel and capital.

## Two books in one Lambda (since 2026-10-22)
| Book | Strategy | Capital (`.env`) | MongoDB | Discord |
|---|---|---|---|---|
| s6 | Strategy 6: S2 / E0 / GAP at 0.55 / 0.55 / 0.05, 9% margin model | `S6_CAPITAL` (₹5L) | `strategy6` | `#strategy6-paper` |
| s7 | Strategy 7: Strategy 6 at 0.45 / 0.45 / 0.05 + monthly straddle (VRP ≥ 2, 20%), 11.6% margin model | `S7_CAPITAL` (₹15L) | `strategy7` | `#strategy7-paper` |

Each run serves both books with one Dhan token (renewed once, kept in `strategy6.state`) and one set of market-data
calls: one option chain, one LTP call per tick for both books' legs, and so on. The books never compete for Dhan's
rate limits. A failure in one book is journalled and posted in that book's channel only; the other book still runs, and
the run then fails so the CloudWatch alarm fires. `invoke.py ... --book s7` runs one book only (STATUS, PAUSE, TEST,
CLEAR_TRADE...). MONTHLY_1015 (10:15) runs for the Strategy 7 book only. Strategy 7's monthly data is thin
(`docs/STRATEGY_7_REALTRADE.md`), so this book is paper-only until its live record exists.

## Rules (`rules.py`, parity-tested against `research_realtrade.py`)
"VIX" is the previous session's India VIX close.

| Sleeve | When | 09:30 entry | Exit | Size |
|---|---|---|---|---|
| S2 | VIX ≥ 15, 2 sessions before expiry | sell CE/PE at ATM ± 1.25 × ATM straddle (skip if > 450 from ATM) | CE+PE ≥ 2 × entry (stop), else 15:15 expiry day | 55% of equity as margin |
| E0 | VIX ≥ 15, expiry day | sell CE/PE at ATM ± 0.5 × straddle | CE+PE ≥ 1.5 × entry, else 15:15 | 55% of equity as margin (11% per lot) |
| GAP | VIX ≤ 13, 4 sessions before expiry | buy ATM CE if spot > previous close, else ATM PE | limit sell at 2 × fill, else 15:15 expiry day | 5% of equity in premium |

Total margin is capped at 90% of equity. **Before every strangle entry the bot asks Dhan's margin calculator** for those exact
legs and lots. If Dhan's figure is above the room left (90% of equity minus open margin), it trades fewer lots or skips, and the
entry message shows "Margin model / Dhan" and "Margin available". Open trades then count Dhan's figure. If Dhan's calculator
is unavailable, the bot enters on the model margin and the message says "Dhan check unavailable". On 2026-10-22 Dhan asked
₹1.72L for one strangle lot against the model's ₹1.33L (9%), so the real check matters.

**Capital: ₹5L since 2026-10-22** (`S6_CAPITAL=500000` in `.env`; ₹3L at the first deploy, then ₹4L). At Dhan's real margin
(~11.6% of notional, ~13.6% on expiry day) ₹3L trades only the gap buy in 2026, ₹4L blocks the expiry-day strangle while the week's
strangle is open, and ₹5L fits both (~₹1.72L + ~₹2.1L). Backtest at real margin from Jan 2024: ₹4L 19.7% a year, ₹5L 23.5%, DD about −12%. **Before every strangle entry the bot asks Dhan's margin calculator** for those exact
legs and lots. If Dhan's figure is above the room left (90% of equity minus open margin), it trades fewer lots or skips, and the
entry message shows "Margin model / Dhan" and "Margin available". Open trades then count Dhan's figure. If Dhan's calculator
is unavailable, the bot enters on the model margin and the message says "Dhan check unavailable". On 2026-10-22 Dhan asked
₹1.72L for one strangle lot against the model's ₹1.33L (9%), so the real check matters. The research weights are 0.45 / 0.45 (for ₹10L). At ₹3L, 0.55 keeps one
S2 lot tradable up to Nifty ~28,200, where 0.45 stops at ~23,077 (see `rules.py`).

**What ₹3L actually trades** (Nifty ~22,400, lot 65):
- **S2:** 1 lot (~₹1.31L margin model).
- **E0:** 1 lot (~₹1.60L) only when no S2 is open and equity is at least ₹2.91L.
- **GAP:** 1–2 lots.

Fixed-size backtest at today's lot and margin: about 19% a year on average, ranging from 3.5% to 50% by year,
with profit factor 1.9. Starting fresh in Jan 2026 it would be +2.8% to date. Expect a slow, uneven start.

## Schedule (Asia/Kolkata, weekdays; holidays skipped in code)
RENEW_TOKEN 06:00 + 18:00 daily · HEARTBEAT 08:45 · ENTRY_0931 09:31 · TICK every minute 09:32–15:16 · EOD 15:40.
No retries; async retries 0; reserved concurrency 1. A TICK with nothing open reads one Mongo document and exits.

## Setup and deploy
```bash
python strategy6_bot/setup_discord.py --test     # creates #strategy6-paper + webhook -> S6_DISCORD_WEBHOOK_URL in .env
# .env also needs S6_CAPITAL=500000 (plus the shared DHAN_*, MONGODB_URI, ALERT_EMAIL)
python strategy6_bot/deploy.py --test            # Lambda Strategy6PaperBot, schedules group strategy6, alarms, INIT, TEST
```

## AWS permissions
The IAM user `dev` needs `iam_policy.json` (the old bot's permissions, scoped to `Strategy6PaperBot*` / `strategy6*`).
Attach it as a managed policy from an admin login (console, or CLI with an admin profile):
```bash
aws iam create-policy --policy-name Strategy6PaperBotDeploy --policy-document file://strategy6_bot/iam_policy.json
aws iam attach-user-policy --user-name dev --policy-arn arn:aws:iam::463870298492:policy/Strategy6PaperBotDeploy
```

## Runbook
```bash
python strategy6_bot/invoke.py STATUS                 # equity, open trades, token validity
python strategy6_bot/invoke.py TEST                   # dry run of today's 09:31 decisions (records nothing)
python strategy6_bot/invoke.py PAUSE | RESUME         # stop / allow new entries (open trades still managed)
python strategy6_bot/invoke.py FLATTEN                # next TICK closes everything
python strategy6_bot/invoke.py SET_TOKEN --token X    # new Dhan token if renewal failed
python strategy6_bot/invoke.py TEST --local           # in-process against the *_dev databases; no Discord posts (add --discord to post)
python -m pytest strategy6_bot/tests -q               # 160 tests (parity tests need the engine cache)
python strategy6_bot/deploy.py --remove               # delete function, schedules, alarms, roles (Mongo kept)
```

## Notes
- **Dhan token:** this bot renews and keeps its own token in Mongo `strategy6.state`. If another bot renews the same Dhan token, one of them ends up with an invalid token, so run only one bot per Dhan account.
- **Running costs:** ₹590/month (Dhan Data API) are charged to paper equity. Lambda, MongoDB free tier and Discord cost nothing. The research used ₹1,990 (VPS setup).
- **Not built yet:** next-morning reconciliation against the replay (`three_sleeve_bot/reconcile.py` has the pattern) and live order placement.
