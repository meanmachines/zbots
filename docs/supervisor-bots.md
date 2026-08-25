# Supervisor / QC bots

A `supervisor`-category bot reviews other bots' recent activity and reports
on it -- it doesn't do task work itself. There's no dedicated supervisor
mechanism in the backend; it's a configuration pattern built entirely on
things that already exist:

- the `supervisor` category (`chore | task | developer | supervisor |
  general`, see `backend/main.py`'s `CATEGORIES`) keeps its worker warm at
  all times, same as `chore`/`developer` bots -- see `_keep_warm_bots()`.
- a routine (the same recurring-job mechanism any other bot uses) drives it
  on a schedule.
- the routine's prompt tells it to use the existing supervisor MCP tools
  (`backend/supervisor_mcp.py`) -- `list_bots`, `get_bot_status`,
  `message_bot` -- to actually look at other bots, then write up findings.
- the routine's delivery already goes through `message_bot`'s own path
  (`notify: true`), so a finished report reaches the user as a real push
  notification the same way any other routine delivery does -- nothing
  extra to wire.

## 1. Create the bot

Either let creation infer the category from a description that reads like
oversight work ("reviews the other bots' recent activity and reports
issues"), or set it explicitly:

```
POST /bots
{
  "name": "qc-bot",
  "title": "QC",
  "description": "Reviews the other bots' recent activity and flags anything that looks wrong.",
  "category": "supervisor"
}
```

## 2. Give it a persona (SOUL.md)

```markdown
# QC

You are the quality-control supervisor for this bot fleet. You do not do
task work yourself -- you watch how the other bots are doing and report on
it.

On each check-in:
- Call `list_bots` to see the current roster.
- For any bot with recent activity, call `get_bot_status` on it (or
  `message_bot` with a specific probing question if a status answer looks
  evasive or generic).
- Note anything that looks wrong: a bot stuck repeating itself, giving up
  early, contradicting its own earlier replies, or clearly not doing what
  its description says it should.
- Write a short report: what you checked, what's fine (briefly), what
  needs a human look (in detail, with which bot and why).

Keep it factual. Don't guess at causes you can't see from the bot's own
replies, and never take an action on another bot's behalf -- your job is to
report, not to fix.
```

Set it via:

```
POST /bots/qc-bot/soul   (or the SOUL.md field in the create/edit modal)
```

## 3. Attach a routine

```
POST /bots/qc-bot/routines
{
  "routine": "daily-qc-report",
  "schedule": "0 9 * * *",
  "prompt": "Run your quality-control check-in now and report your findings."
}
```

`target_bot` is omitted, so the report lands in `qc-bot`'s own chat (and, via
the routine-delivery path, as a push notification). Point `target_bot` at a
different bot instead if the report should land somewhere else, e.g. an
existing daily-briefing bot.

## Verifying it's real

`get_bot_status`/`message_bot` are genuine turns against the target bot, and
`list_bots` reads the live roster -- a report only references bots and
activity that actually exist. Trigger it once by hand instead of waiting for
the schedule (`/cron run <job_id>`, or just message `qc-bot` directly with
the routine's own prompt) and confirm the report names real bots and
describes their real recent replies, not generic filler.
