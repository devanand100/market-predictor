# Plan: trade-journal dashboard served from Lambda (to build later)

> **Removed 2026-10-23.** The bot was taken off AWS on 2026-10-22 (replaced by `strategy6_bot/`) and its code was
> deleted from the repo. This dashboard plan was never built; it is kept for reference. The code is in git history:
> `git show 46a7036:three_sleeve_bot/<file>` or `git checkout 46a7036 -- three_sleeve_bot`. Real-price results:
> `STRATEGY_5_REAL_CHECK.md`.

Goal: the same dashboard as the Claude artifact "Strategy 5 trade journal"
(https://claude.ai/artifact/FSCNSS5GuaGhMdPNEsxM73), but reading the bot's MongoDB journal, so paper trades can be
viewed (and compared with the backtest) in a browser.

## What already exists (2026-10-21)
| Piece | File | State |
|---|---|---|
| Page (HTML/CSS/JS, d3) | `dashboard/index.html` | Same page as the artifact, unchanged |
| Artifact runtime stand-in | `dashboard/dash_shim.js` | `dash.data/onData/calc/params/setParams/colors`; reads inline rows or `${DASH_API}/data/<id>` |
| Design tokens | `dashboard/theme.css` | Light + dark |
| Sample data | `dashboard/sample/trades.json`, `daily.json` | Rs 15L backtest, 450 trades |
| Local preview | `dashboard/build_preview.py` | `python three_sleeve_bot/dashboard/build_preview.py [--mongo]` → `dashboard/preview.html` |
| Mongo → rows | `api.py` | `trades_rows(src)`, `daily_rows(src)`; tested against the sample schema |

## Database: no schema change needed, only additive fields (already added)
The journal already held almost everything. Added on 2026-10-21 (computed inside existing writes, no extra writes):
- `trades.px_in` (entry: gap fill, or CE+PE fills), `trades.px_out`, `trades.ret_pct` (net / equity before),
  `trades.equity_after` (realised equity after the trade)
- `days.running_today`, `days.closed_n`

| Dashboard column | From |
|---|---|
| trades: id, sleeve, entry, exit, lots, exit_reason, pnl | `_id`, `sleeve`, `entry_d`, `exit_d`, `lots`, `exit_reason`, `net` |
| trades: detail | built from `legs` + `in` (strikes, straddle/width, gap, VIX, VRP) |
| trades: entry_px, exit_px, ret_pct, equity_after | new fields above |
| daily: date, trade_pnl, running_costs, trades_closed, equity, equity_mark | `days.d`, `realised_today`, `running_today`, `closed_n`, `equity`, `mark` |

Differences to keep in mind:
- Paper L2 is one trade **per tranche** (`key = L2|date|tN`, cycle in `ckey`); the backtest export has one row per
  weekly cycle. Fine for the page; for a paper-vs-backtest view, group paper rows by `ckey`.
- `days` exists only from the first EOD the bot runs; the heatmap and curves start there.

## Architecture
- **Separate Lambda `ThreeSleeveDashboard`** (python3.13, arm64, 128 MB), not the trading function: the trading
  function has reserved concurrency 1, so dashboard requests must never queue behind or block a TICK.
- **Lambda Function URL** (no API Gateway, free): `GET /` returns the page (index.html + shim + theme, data fetched
  client-side), `GET /data/trades?src=paper|research`, `GET /data/daily?src=paper`.
- **Access:** Function URL `AuthType NONE` + a long random `DASHBOARD_KEY` (env var) required as `?key=` on every
  request (constant-time compare; 403 otherwise). Optional later: CloudFront + Lambda@Edge auth.
- **Mongo:** read-only user restricted to `three_sleeve` (find only); `maxPoolSize=1`; two indexed queries per load.
- **Backtest comparison:** `import_backtest.py` loads the backtest export as `trades.src="research"`; the page gets
  a Paper / Backtest switch (`?src=`), and a later "Paper vs backtest" tab joins on `ckey` and the `recon` collection.
- **Caching:** `Cache-Control: max-age=60` on data (EOD changes once a day), so refreshes don't hit Mongo.

## Work list
1. `dashboard_lambda.py`: routes above, key check, JSON via `api.py`, page assembled like `build_preview.py`.
2. `deploy.py --dashboard`: function, Function URL (`create_function_url_config`, `AuthType NONE`, CORS GET only),
   resource policy (`lambda:InvokeFunctionUrl`), env (`MONGODB_URI` read-only, `DASHBOARD_KEY`), log retention.
3. `import_backtest.py` (research rows, same shape) + Paper/Backtest switch in the page.
4. Optional tab: open positions with live MTM from `state.open` (one extra read, no Dhan calls from the dashboard).
5. Tests: route + key check, JSON shape equals `dashboard/sample`.

## AWS permissions to add for user `dev` when building it
`lambda:CreateFunctionUrlConfig, UpdateFunctionUrlConfig, GetFunctionUrlConfig, DeleteFunctionUrlConfig` and the
existing create/update/AddPermission actions on `function:ThreeSleeveDashboard`; `iam:PassRole` on the
dashboard's role; `logs:CreateLogGroup, PutRetentionPolicy` on `/aws/lambda/ThreeSleeveDashboard*`.
No fallbacks: the deploy stops and names any missing permission.
