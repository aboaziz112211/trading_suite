# Trading Suite — operational guardrails (READ FIRST)

This project deploys to **Render free tier** (500 pipeline minutes/month, no
persistent disk, frequent worker restarts) and auto-deploys on every push to
`main`. Those constraints have caused real money/time losses. Follow these
rules before executing anything that touches metered resources.

## 🛑 Hard rules — check BEFORE executing, not after

1. **Never put a GitHub commit (or any deploy-triggering action) on an
   automated / recurring / per-request hot path.**
   - Every push to `main` triggers a Render auto-deploy → consumes pipeline
     minutes. A loop here drains the monthly cap and blocks ALL deploys.
   - Past incident: the visit counter auto-committed `visit_counts.json` →
     each commit deployed → restart → repeat. Burned all 500 minutes twice.
   - If something must persist across redeploys, write to **local disk only**
     and restore from the committed snapshot on boot. Commit to GitHub ONLY
     from an explicit, admin-triggered, infrequent endpoint.

2. **Any in-memory throttle/guard is worthless on this stack** — the worker
   restarts constantly (every deploy, idle spin-down, free-tier cycling) and
   wipes memory. A "once per day" timer stored in a global resets to 0 on
   every restart and never actually limits. If you need a durable throttle,
   read the last-action timestamp from a **persisted file**, never memory.

3. **Trace every automated action through a restart + failure before shipping.**
   Ask: "What re-runs this? What state does it rely on surviving a restart?
   What happens if it fires twice / in a loop?" If you can't answer, don't
   ship it.

4. **Treat metered resources as careful operations:** Render deploys/builds,
   GitHub commits, Polygon/Telegram/Yahoo API calls, anything with a quota.
   Don't treat them like free local operations.

5. **Prefer removing the hazard over throttling it.** The correct fix is
   usually "don't do the risky thing automatically," not "do it less often."
   Reach for the simplest design that eliminates the failure mode.

## Stack facts (so you don't re-learn them the hard way)

- **Auto-deploy is ON** by default. Every `main` push = a deploy = minutes.
- **No persistent disk.** Anything written to the container's filesystem is
  lost on redeploy/restart. GitHub repo is the only durable store — but
  committing to it triggers a deploy (see rule 1).
- **Local feeds** (`sar_feed.py`, `polygon_feed.py`) run on the user's PC and
  push to `/api/sa/push` and `/api/us/push`. They're kept alive by Windows
  scheduled "watchdog" tasks (`C:\TradePulse\*_watchdog.ps1`) that revive a
  dead feed within 5 min during market hours.
- **Closing brief** auto-publishes 15:20 AST Sun–Thu via a Windows task that
  does its own fresh push then calls `/cron/closing-brief`.
- **Secrets** (tokens, API keys) live in Render env vars / user env vars —
  NEVER commit them to this public repo.

## Markets / timing

- TASI (Saudi): trades **Sun–Thu 10:00–15:00 AST**. Fri+Sat = weekend.
- US: trades **Mon–Fri 16:30–23:00 AST** (= 09:30–16:00 EDT).
- The "Analyze a stock" topbar button routes visitor requests to the admin's
  Telegram via a bot (`TELEGRAM_BOT_TOKEN` + `ANALYZE_CHAT_ID` env vars).

## Before you push

- Is anything you added recurring or automated? → apply rules 1–3.
- Does it touch a metered resource? → apply rule 4.
- Could it fire in a loop or after a restart? → trace it through, or don't ship.
