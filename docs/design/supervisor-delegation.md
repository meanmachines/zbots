# Design: async task delegation for a supervisor bot

Status: **built** (`supervisor_mcp.py`'s `delegate_task` tool, plus
`_run_delegated_task`/`_fire_and_forget`), unit-tested
(`tests/test_supervisor_mcp.py`), verified live end-to-end on
`zbots-dev`. One real difference from the original design below:
`delegate_task` takes an explicit `from_bot` parameter rather than
inferring the caller -- MCP tool calls arrive at this server with no
caller identity attached, so there's no channel to detect it from; the
calling bot has to say who it is.

## The actual goal

Not "one bot with two models spliced together." The real target,
per the user's own framing: a **supervisor bot** -- fast, low-latency,
eventually voice-enabled -- that talks with the user, understands what
they want, and hands real work off to other bots (each free to run a
bigger/slower/more accurate model) that do the work in the background.
The supervisor stays responsive; the user never feels the latency of
whichever specialist bot is actually doing the work.

This is not a new architecture to invent. It's the `bot-supervisor` MCP
server (`backend/supervisor_mcp.py`) -- built and verified working
end-to-end this session -- extended with one missing piece.

## The gap in what exists today

`message_bot(name, message)` is synchronous: the calling bot's own agent
loop blocks on the full HTTP round trip to `send_to_bot()`, which itself
waits for a complete conversational turn from the target bot. Verified
live: a single tool-use turn through this path took ~50-60s end to end.
A supervisor bot delegating "heavy work" through `message_bot()` would
block for exactly as long as the worker takes -- the opposite of staying
responsive. `list_bots()` and `get_bot_status()` have the same shape.

What's missing is a **fire-and-forget** delegation primitive: hand off a
task, get an immediate acknowledgment, keep talking to the user, and
have the result show up on its own once the worker finishes -- without
the supervisor's own turn blocking on it.

## Design

### New tool: `delegate_task(from_bot, to_bot, task)`

Added to `supervisor_mcp.py` alongside the existing three tools, not
replacing `message_bot()` (that stays -- it's still correct for "ask
another bot something and use the answer in my own reasoning right
now").

```
delegate_task(bot_name: str, task: str) -> {"task_id": str, "status": "delegated"}
```

Implementation shape:
1. Generate a `task_id`.
2. Kick off `send_to_bot(bot_name, task, ...)` as a background `asyncio`
   task (`asyncio.create_task`, not awaited) -- reusing the *exact*
   existing `engine.send_to_bot()`, including its resilience/rollover
   logic. Nothing new here; same function this session already proved
   works.
3. Return immediately with the `task_id`. The supervisor's own turn
   continues (or ends) without waiting.
4. When the background task completes, deliver the result back into the
   *supervisor's own active session* as a new turn -- not a return
   value nobody's listening for. Concretely: call `send_to_bot()` again,
   this time addressed to the supervisor bot itself, with a message
   like `"[delegated task <task_id> from <bot_name>] <result>"`. This
   reuses `send_to_bot()`'s existing session-continuity machinery
   instead of inventing a second delivery mechanism -- the supervisor
   sees the result the same way it'd see any other incoming message,
   next time it's asked to respond.

### Why deliver back through a real turn, not a callback/webhook

Considered and rejected: a raw callback/webhook into the supervisor's
session bypassing `send_to_bot()`. Rejected because it would need its
own session-selection and resilience logic duplicated from
`engine.py` -- exactly the kind of parallel-implementation risk
`engine.py`'s own module docstring already warns against for the
handler-calling layer. Routing task completion through the same
`send_to_bot()` path means every fix already made to that path (rollover,
corrupted-reply retry) automatically covers delegated-task delivery too.

### What the supervisor bot's own persona needs

A system-prompt-level instruction (set via the existing `/bots/{name}/soul`
endpoint, no new mechanism needed): prefer `delegate_task` over
`message_bot` for anything that sounds like real work (multi-step
research, code changes, long-running analysis); keep `message_bot` for
quick factual questions to another bot where waiting a few seconds is
fine. This is prompt engineering, not new infrastructure.

### Explicitly out of scope for this phase

- **Voice.** The user's own framing places voice as a later part of the
  supervisor-bot plan, not this delegation piece. Nothing here assumes
  or blocks voice; it's a text-turn-level design.
- **A fast "talker" model swap mid-turn.** No token-level speculative
  decoding, no dual-model splicing within one bot. The "fast" half of
  this design is just: pick a small/cheap model (Phi-4-class or similar)
  for the supervisor bot's own profile, same as configuring any other
  bot's model today -- nothing new to build for that part.
- **Task cancellation, retries, or a task history UI.** A real product
  surface eventually, not needed to test whether the core idea works.

## Testing as a side feature

Built behind nothing more than "a new tool the supervisor bot's persona
is told to prefer" -- `message_bot`/`list_bots`/`get_bot_status` stay
completely untouched, so this can't regress anything already working.

Test plan on `zbots-dev`:
1. Create two bots: `supervisor` (fast/cheap model) and `worker` (the
   existing default model).
2. Give `supervisor` a persona instructing it to delegate real work.
3. Ask `supervisor` something that requires real work from `worker`
   (e.g. "ask worker to summarize its own last three messages").
4. Confirm: `supervisor` responds quickly (an acknowledgment, not a
   50s wait), and the delegated result shows up in `supervisor`'s own
   session history once `worker` finishes -- verified by polling
   `/bots/supervisor/messages` after the fact.

**Keep it** if that round-trip works reliably and the perceived latency
win is real. **Leave it** (delete `delegate_task`, revert the persona
guidance) if it turns out to be unreliable or the win doesn't materialize
in practice -- no other part of zBots depends on it either way.
