# three_sleeve_bot: Strategy 5 paper trader

Paper-trades Strategy 5 (`docs/PORTFOLIO_STRATEGY.md`) live: weekly staggered strangle (L2) + gap buy (VIX ≤ 15)
+ monthly straddle (VRP ≥ 2). **No orders are placed.** Runs on AWS Lambda with Dhan market data, journals
everything to MongoDB and posts **one Discord message per run** to a single channel.

## Status (2026-10-20)
- Code complete, 145 tests passing (parity with the research code, full simulated week, write budget).
- Discord channel `#three-sleeve-paper` and its webhook are created (`DISCORD_WEBHOOK_URL` in `.env`).
- AWS: Lambda function and role created. **Deploy paused**: reserved concurrency needs the account Lambda limit
  raised from 10 (request `6ceaaf1faab043ccbb5c05894992bbb04eHF3V11`, 1000, PENDING). After approval, run
  `python three_sleeve_bot/deploy.py --test`. Schedules don't exist yet, so nothing runs.
- Paper capital Rs 15L (`CAPITAL`): at Nifty ~25,000, Rs 10L can't size the L2 sleeve (needs >= 3 lots).

## Files
| File | Role |
|---|---|
| `lambda_function.py` | Handler, actions, run wrapper (lease, error dedupe, one Discord message per run) |
| `rules.py` | Pure strategy rules shared by live and replay (sizes, strikes, stops, targets, filters) |
| `replay.py` | Rules on 1-min bar closes (research fill model): parity tests + reconciliation |
| `reconcile.py` | Next-morning replay of yesterday on Dhan 1-min data vs the paper trades |
| `costs.py` | Copy of `../cost_model.py` (a test keeps them identical) |
| `dhan.py` | Dhan v2 REST client (read-only), token renew; at most one retry |
| `journal.py` | MongoDB collections and writers (free-tier budget: < 40 writes a day) |
| `notify.py` | Discord webhook; collects embeds and flushes once per run |
| `market_calendar.py` | IST, NSE holidays, weekly cycle, monthly expiries |
| `deploy.py` / `invoke.py` / `setup_discord.py` | Deploy (no fallbacks), manual actions, one-off Discord setup |
| `api.py` + `dashboard/` | Trade-journal dashboard (same page as the Claude artifact): `python three_sleeve_bot/dashboard/build_preview.py [--mongo]` → `dashboard/preview.html`. Lambda hosting planned in `docs/DASHBOARD_PLAN.md` |

## Schedule (EventBridge Scheduler, Asia/Kolkata, weekdays; holidays skipped in code)
RENEW_TOKEN 06:00 + 18:00 daily · RECONCILE 08:15 · HEARTBEAT 08:45 · SNAPSHOT_0920 09:21 · ENTRY_0931 09:31 ·
MONTHLY_1015 10:15 · TICK every 5 min 09:36–15:31 · EOD 15:40. No retries; Lambda async retries 0; concurrency 1.

## MongoDB (`three_sleeve`; `three_sleeve_dev` for local runs)
`state` (1 doc: token, flags, open positions, snapshot, MTM path, error counts) · `decisions` (every taken/skipped
rule with inputs and reason) · `trades` (`src` paper / research; legs with fills, itemised charges, net P&L, `key`
per trade and `ckey` per cycle for backtest comparison) · `days` (EOD equity, drawdown, margin model vs Dhan) ·
`recon` (paper vs replay) · `errors` (first occurrence per day) · `runs` (TTL 120 days).

## Runbook
```bash
python three_sleeve_bot/invoke.py STATUS                 # state, equity, open trades, token validity
python three_sleeve_bot/invoke.py TEST                   # dry run of today's decisions (records nothing)
python three_sleeve_bot/invoke.py PAUSE | RESUME         # stop / allow new entries (open trades still managed)
python three_sleeve_bot/invoke.py FLATTEN                # next TICK closes everything
python three_sleeve_bot/invoke.py SET_TOKEN --token X    # new Dhan token if renewal failed (no redeploy)
python three_sleeve_bot/invoke.py RECONCILE --recon-date 2026-10-19
python three_sleeve_bot/invoke.py TEST --local           # in-process against three_sleeve_dev
python -m pytest three_sleeve_bot/tests -q               # 145 tests (parity tests need the engine cache)
python three_sleeve_bot/deploy.py [--test | --remove]
```
`.env` keys: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` (seed; the bot keeps the renewed token in Mongo), `MONGODB_URI`,
`DISCORD_WEBHOOK_URL`, `CAPITAL`, `ALERT_EMAIL`; optional `MONGO_DB`, `EXTRA_HOLIDAYS`, `AWS_REGION`.

## AWS permissions
The IAM user that runs `deploy.py` needs (inline-policy space on a user is 2,048 characters in total, so use a
managed policy):
- `iam:CreateRole, GetRole, UpdateAssumeRolePolicy, DeleteRole, AttachRolePolicy, DetachRolePolicy, PutRolePolicy,
  DeleteRolePolicy, ListRolePolicies, ListAttachedRolePolicies, PassRole` on `role/ThreeSleevePaperBot*`
- `lambda:CreateFunction, GetFunction, GetFunctionConfiguration, UpdateFunctionCode, UpdateFunctionConfiguration,
  DeleteFunction, PutFunctionEventInvokeConfig, PutFunctionConcurrency, InvokeFunction, AddPermission,
  RemovePermission` on `function:ThreeSleevePaperBot`
- `scheduler:CreateScheduleGroup, DeleteScheduleGroup, CreateSchedule, UpdateSchedule, DeleteSchedule, GetSchedule,
  ListSchedules` on `schedule-group/three-sleeve` and `schedule/three-sleeve/*`
- `logs:CreateLogGroup, PutRetentionPolicy` on `/aws/lambda/ThreeSleevePaperBot*`
- `cloudwatch:PutMetricAlarm, DeleteAlarms` on `alarm:ThreeSleevePaperBot-*`; `sns:CreateTopic, Subscribe,
  ListSubscriptionsByTopic` on `three-sleeve-alarms`
- Account Lambda concurrency above 10 (reserved concurrency 1 must leave 10 unreserved).

## Known differences from the backtest (measured daily by RECONCILE)
- Fills use live LTP at 09:31 / 10:15 / each tick instead of the 1-min / 60-min bar close.
- Gap-buy target is checked every 5 min (research: every 1-min close).
- L2 tranches are whole lots (`N//3`); research used 216 units per tranche.
- Monthly sleeve: research used 60-min bars with some model-priced exits.
