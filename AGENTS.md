# market-predictor: agent guide

The full guide is `CLAUDE.md` (status, data traps, file map, commands, conventions). Read it first.

## Rules
- **Project knowledge lives in this repo, not in an agent's private memory.** Results, decisions, live/paper setup,
  open plans and data traps go into `CLAUDE.md`, `docs/` or the bot's README in the same task, so they survive a new
  laptop or a different agent. Agent memory holds at most short personal preferences.
- **No silent fallbacks.** When blocked by a missing permission, stop and give the exact permission to add.
- Bot deployment style, Dhan-token rule and research conventions: see the Rules section of `CLAUDE.md`.
