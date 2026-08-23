# Changelog

All notable changes to zBots are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions are
tagged on `main`.

## [Unreleased]

### Added
- Four new `bot-supervisor` MCP tools -- `list_routines`, `pause_routine`,
  `resume_routine`, `delete_routine` -- so a bot can manage scheduled
  routines (cron jobs) directly in conversation, not just through the
  Routines admin page. Real bug found live: hermes-agent's own native
  `cronjob` tool IS declared in the api_server platform's default toolset
  (confirmed in `toolsets.py`), but never actually reaches an agent
  running through zBots' embedded engine -- traced the real tool_defs
  list a live chat turn receives and confirmed `cronjob` is absent while
  other same-toolset tools (`memory`) are present. Root cause: the
  cronjob tool needs a scheduler reachable in its own process, and the
  embedded chat engine (`main:app`) is a separate process from the one
  that actually owns it (`hermes gateway run`) -- the same process split
  Connectors work surfaced earlier. Rather than chase that gate, these
  wrap zBots' own already-real `/cron` proxy (the same routes the
  Routines page itself uses) the same way every other bot-supervisor tool
  wraps a real zBots endpoint -- message_bot/create_bot were never
  reimplementing anything either. Accepts a routine by name (e.g.
  "bobby-checkin") or id, with a close-match suggestion on a typo, same
  pattern as `_require_bot`. Verified live end-to-end through real
  conversation, not just by reading the code -- paused bobby-checkin via
  chat, confirmed the real job state actually changed, resumed it the
  same way.
- A small, transient status line while a bot is using a tool (e.g.
  "Messaging default...", "Creating routine...", "Thinking...") -- the
  same idea as the desktop app's own tool-progress indicator. Real
  hermes-agent streaming events (`tool.started`/`tool.progress`) were
  already arriving over the wire; the frontend only ever handled
  `assistant.delta` and silently dropped the rest. Real bug found live
  building this: delta text narrating an upcoming tool call ("I'll use
  the message_bot tool to...") streams BEFORE the tool.started event for
  it, so a naive "only show status before any delta" guard never fired --
  the narration bubble was already on screen. Fixed by discarding that
  bubble the moment a real tool call starts (the same thing the
  persona's response-style guardrail already asks the model not to write
  in the first place -- this is the deterministic backstop for when it
  does anyway) and showing the clean status line instead; a fresh bubble
  opens once the real, final delta run begins. `bot-supervisor`'s own
  tools and hermes' action-based tools (cronjob, memory) get specific,
  friendly labels; the tool_search bridge (tool_search/tool_describe/
  tool_call) resolves through to the real underlying tool where possible;
  anything else falls back to a humanized version of its raw name rather
  than being hidden, so a newly installed MCP server's tools show
  something sensible with zero frontend changes needed. Verified live
  with a real browser, not just by reading the code -- watched the label
  progress through an actual multi-tool-call turn and confirmed it clears
  correctly once the real answer starts.

### Fixed
- A scheduled routine's own internal trigger text (e.g. "This is your
  scheduled 5-minute check-in trigger...") rendered as a real "user"
  chat bubble, as if the person had typed it themselves -- message_bot
  has to post SOME real inbound turn for the target bot to answer, and
  with no way to distinguish it from a genuine message, the raw internal
  prompt was just shown. Fixed with an `[internal-trigger]` marker
  prefix: `collapseToFinalTurns` (frontend/app.js) now recognizes and
  hides a user turn carrying it -- ends the previous turn same as a real
  user message would, but never renders itself, so the reply that
  follows just shows up on its own, correctly matching a proactive
  check-in nobody "asked" for. Nothing about delivery changes -- this
  only controls what's displayed. Applies going forward only; the three
  check-ins that fired before this landed keep showing their raw
  trigger text (predate the marker).
- Cron routines that deliver into a specific bot's chat (the "ask me
  something every N minutes" pattern) now actually work. Root cause,
  confirmed by checking every real hermes-agent delivery path rather than
  guessing: `deliver: bot-chat:<name>` needs a real hermes *profile*
  (zBots bots are sessions under one shared profile, not separate
  profiles -- `Profile 'assistant' does not exist`); `deliver: origin`
  resolves cleanly but has no working adapter for the `api_server`
  platform zBots' own chat runs on (reported success, delivered nothing --
  confirmed live, message count never moved); `cron/scheduler.py` has zero
  references to `api_server` at all. None of hermes-agent's native cron
  delivery targets a zBots bot's session. The real fix needed no new
  code: `bot-supervisor`'s own `message_bot` tool already does exactly
  this (inject as an inbound turn, get the target bot's own real,
  persisted reply) -- so the fix is entirely in the job's own prompt
  (call `message_bot` directly, `deliver: local`) rather than hermes'
  external delivery mechanism at all. Added `PUT /cron/{id}` (zBots' side
  was missing the update proxy for this) to make jobs actually editable
  instead of delete-and-recreate.
- `stale_model_lock_rolls_over`'s detection was too narrow -- it only
  matched the exact wording "not a valid model" and missed a second,
  differently-worded rejection from the same class of bug, confirmed live
  right after the first fix shipped ("HTTP 400: Model ID 'deepseek-chat'
  is ambiguous -- it matches multiple models"). Broadened to match on
  shape alone (any reply starting with "HTTP <code>:"), matching what the
  check's own docstring already said it should be doing -- no real
  assistant reply opens with that literal string, regardless of which
  provider produced the underlying rejection or how it worded it.
- A new persona guardrail, `RESPONSE_STYLE`, alongside `BRANDING_SAFETY`
  (both now applied together by `with_branding_safety()`): real bug found
  live, a blocked or uncertain bot would write its own reasoning process
  out as the reply itself ("Let me verify whether I can set up a cron
  job... Let me think about what I can realistically do...") instead of a
  short status update, and separately stack multi-part clarifying
  questions into one long paragraph instead of asking the one thing that
  actually unblocks it. Not a technical reasoning-field leak
  (`reasoning_echo` defaults off) -- the model's actual visible answer was
  just written in a narrating-out-loud style. Pushed live to all four
  existing bots (default, mandy, gg, assistant), not just future ones.
- Two real bots (`gg`, `assistant`/"Bobby") were found live with their
  soul's opening line still reading "You are Hermes Agent, an intelligent
  AI assistant created by Nous Research" -- the exact leak
  `persona.py`/`redact_branding_leaks` exists to catch in replies, but
  sitting directly in the system prompt itself (predating this session's
  branding-safety work, never revisited). `default` and `mandy` were
  already correct. Fixed by replacing the leaked opening line with the
  real zBots one on both affected bots' souls, keeping everything else
  each bot already had.
- The `bobby-checkin` cron routine (a user-created "every 5 minutes, ask
  me what I need" job) was confirmed live to actually fire correctly on
  schedule, but its delivery has failed every time since creation:
  `bot-chat:assistant` delivery expects a real hermes-agent *profile*
  named "assistant", but zBots bots are sessions under one shared profile,
  not separate hermes profiles -- a genuine architecture mismatch between
  hermes-agent's native cron delivery and zBots' bot model, not something
  wrong with the job itself. Paused rather than left silently failing
  every 5 minutes; a real fix needs a zBots-native cron delivery path that
  resolves a bot name to its session the same way send_to_bot() already
  does, not hermes' own profile-based delivery.

### Added
- A Connectors page (nav: Platform -> Connectors), the same self-service
  pattern as Models/MCP Servers: a thin proxy (`GET/PUT /connectors[/{id}]`,
  `POST /connectors/{id}/test`) over hermes-agent's own real
  `/api/messaging/platforms` API -- the same one the desktop app's Channels
  page uses. The full platform catalog (Telegram, Discord, WhatsApp, Slack,
  Signal, Matrix, Mattermost, and everything else in `gateway.config
  .Platform`, plus any installed plugin platform), field metadata
  (prompt/help/docs links/required/password), and validation are all real,
  already-built hermes-agent code -- the page renders whatever the API
  returns rather than hardcoding a platform list. Verified live end-to-end
  including the real backend's own token-format validation rejecting a
  fake Telegram token with its actual error message.
- The messaging-platform gateway (`hermes gateway run`) now runs as a
  fourth in-container process, so a platform configured on the Connectors
  page above actually connects instead of sitting saved-but-inert (`hermes
  gateway start` refuses inside Docker and points here instead). Confirmed
  live it starts cleanly alongside the existing three processes with no
  port conflicts, but roughly doubles idle memory use (~197MB -> ~365MB
  with zero platforms enabled) -- too tight against the previous 384MB
  container limit, so the memory budget was raised to 1024MB before wiring
  this in rather than after.

### Corrected
- An earlier changelog entry claiming external MCP server tools (like
  `bot-supervisor`'s) bypass hermes-agent's `tool_search` deferred-listing
  cap was wrong. Re-verified live: `tools/tool_search.py`'s own module
  docstring says MCP tools are exactly what it's for, and a direct check
  against the real registry confirmed every one of `bot-supervisor`'s
  tools is correctly classified deferrable and the catalog listing is
  active and bounded (`listing_form: full`, capped at
  `listing_max_tokens`). The earlier docstring-trimming work wasn't
  wrong to do, but it wasn't fixing the token-cost driver it was credited
  with -- a real turn's ~15,700-input-token floor persists even now, on a
  genuinely fresh session, with zero conversation history. Where that
  floor actually comes from is still an open question, not this.

### Fixed
- `POST /api/model/set` (already wired as zBots' own `/models/activate`)
  is hermes-agent's real, native way to point the main model at ANY
  provider it understands -- including a first-class OpenRouter
  integration (`provider: openrouter` + `OPENROUTER_API_KEY`), not just
  the ones with a `providers.<id>` custom-endpoint entry. Using that
  directly instead of wrapping OpenRouter in a fake custom endpoint is
  also what surfaced the collision-guard gap above (`openrouter` isn't in
  `PROVIDER_REGISTRY`/`ALIASES` -- it's a separate, hardcoded exclusion
  set in `agent/agent_init.py`) and the stale-model-lock bug below.
- `POST /providers`'s built-in-name collision guard now also rejects
  `openrouter`/`custom`/`auto` -- routing-mode names hardcoded as a
  special exclusion set in `agent/agent_init.py`, not present in
  `PROVIDER_REGISTRY`/`ALIASES` so the original guard missed them. Real
  bug found live: a custom endpoint saved as `openrouter` hit that
  exclusion, so the missing-credentials fail-fast path never fired --
  every message on it 500'd with a raw traceback instead of a clear
  error.
- A new resilience check, `stale_model_lock_rolls_over`: switching the
  *global* active provider (Models page) doesn't clear an existing
  session's own locked model id (this is documented, intentional
  upstream behavior -- `/api/model/set` only affects new sessions), so
  that session's next message sends the OLD provider's stale model
  string to the NEW provider. When the new provider is merely confused
  by it rather than unreachable, it answers 200 with its own rejection
  delivered AS the reply text ("HTTP 400: nvidia/Qwen...-NVFP4 is not a
  valid model ID") instead of a real HTTP failure -- confirmed live
  switching to OpenRouter with an existing zbots-provider session, and
  status-code-only checks like `server_error_rolls_over` can't see it.
  Also fixed a real gap this exposed in `send_to_bot()` itself: it only
  ever acted on a `SAME_SESSION` decision from its reply-aware
  `resilience.evaluate()` call, never `ROLLOVER` -- so even with the new
  check registered, the stale-lock reply would have reached the user
  unhandled. The streaming path gets the identical detection (a
  stale-lock rejection never raises `event: error`, it arrives as an
  ordinary-looking `assistant.delta`, so the frame-shape check alone
  couldn't catch it either) via the same regex, applied during the
  per-frame redaction pass since both need the same decoded delta text.
- The entire mobile responsive stylesheet (the `@media (max-width: 860px)`
  block) sat too early in `styles.css`, before ~600 lines of unconditional
  desktop rules -- with equal specificity, CSS resolves a tie by source
  order, so every one of those later desktop rules silently won back over
  its mobile override on an actual phone. Confirmed live: `#app`'s mobile
  single-column layout never took effect, leaving the chat pane squeezed
  into a leftover ~320px column instead of the real ~390px+ viewport width
  on a phone, which in turn left no room in the chat header for the model
  name pill (`flex-shrink: 0`, no truncation) -- it rendered on top of the
  header's action icons, both unreadable. Moved the whole media-query
  block to the end of the file (standard CSS practice: an override block
  needs to come after what it overrides when specificity is equal) and
  made the model pill actually truncate with an ellipsis instead of
  refusing to shrink, so a long custom-provider model name can't repeat
  this. Verified with real Chromium screenshots at a 393px phone viewport,
  before and after, not just by reading the CSS.

### Added
- Real per-token streaming: chat replies now stream live from the engine's
  own SSE handler (`_handle_session_chat_stream`) instead of waiting for
  the full reply and sending it as one block. Runs the real handler
  in-process by giving it a custom writer (the same mechanism
  `aiohttp.test_utils.make_mocked_request`'s own `writer=` parameter
  exists for -- aiohttp's own test suite drives streaming handlers this
  way) that captures each write() into a queue instead of a socket, so no
  wire-format or frontend changes were needed: the frontend already only
  acts on `assistant.delta` events, and the real handler already emits
  exactly that.
- Along the way, a real bug in the engine itself: a session's SECOND
  message onward reliably failed with "No LLM provider configured"
  whenever the active model was a custom endpoint (`providers.<id>` in
  config.yaml) -- reproduced for both zBots' own self-hosted provider and
  a freshly-added one, so not specific to any one provider. Root cause:
  the engine persists a session-level model lock after the first reply,
  and a custom endpoint's lock round-trips through that as the bare
  string `"custom"` -- enough to know it was a custom endpoint, not which
  one, so the next message's credential resolution has nothing to
  authenticate with. `send_to_bot()`'s existing rollover-on-failure logic
  had been silently absorbing this the entire time chat has been in use
  -- every reply after a session's first was actually served by a fresh,
  silently-rolled-over session underneath, which is why this was never
  visibly noticed. The streaming path now gets the same protection
  (`engine.stream_to_bot()`'s own rollover, retried once): the frontend
  already ignores every SSE event except `assistant.delta`, so a failed
  first attempt's setup/error frames are invisible and a rolled-over
  retry looks like one continuous stream. The underlying hermes-agent bug
  itself (session-lock serialization losing which custom endpoint it was)
  is not fixed here -- that's a real upstream fix, not a zBots workaround.
- Provider self-service (`POST /providers`, `/providers/{id}/activate`,
  `POST /models/activate`, `DELETE /providers/{id}` -- the same add/edit/
  activate/delete flow the Models page already exposed) now calls
  `engine.invalidate_adapter()` after every mutation, same fix already
  applied to `create_bot`/`update_bot`: a provider added or activated
  through this flow used to have no effect on chat until the container
  restarted.
- `POST /providers` rejects a custom endpoint name that collides with a
  built-in provider hermes-agent's own resolver recognizes (`deepseek`,
  `qwen`, `groq`, ...), with a message suggesting a non-colliding name.
  Real bug found live: a custom endpoint saved as "deepseek" got silently
  routed through hermes-agent's built-in `deepseek` overlay instead of the
  saved entry's own `base_url`/`key_env` -- same slug, and the resolver
  checks its built-in registry first. Every chat request then failed auth
  looking for `DEEPSEEK_API_KEY`, a variable the self-service form never
  asked the user to set. The reserved-name list is imported from
  hermes-agent's own `PROVIDER_REGISTRY`/`ALIASES`, not duplicated.

### Changed
- `supervisor_mcp.py`'s tool docstrings trimmed to what a model actually
  needs to use each tool -- the "why"/incident history moved to code
  comments instead. Unlike hermes-agent's own built-in tools (routed
  through a capped, deferred `tool_search` catalog), an external MCP
  server's tool descriptions are sent to the model in full on every
  turn, uncapped. Measured live: a trivial "what is 15 plus 27" cost
  31,337 input tokens with the verbose docstrings attached, 15,673 with
  the trimmed ones -- same question, same bot, same provider, ~50%
  fewer tokens. Bundled skills (`no_skills`) had zero measurable effect
  on this cost, so this -- not the skill catalog -- was the real lever.

### Added
- Frontend: a bot's chat now shows only each exchange's real final
  answer, not every intermediate "let me check..." narration step along
  the way -- the agent loop's own multi-step tool-orchestration turns
  are indistinguishable in shape from a genuine final answer, so without
  this every one of them rendered as its own bubble. Verified against a
  real conversation: 33 raw turns collapsed to the correct 14.
- `supervisor_mcp.py`'s `message_bot`/`get_bot_status`/`delegate_task`
  now check the target bot actually exists before doing anything, with a
  suggested close-name match if it doesn't. Real bug found live:
  messaging a name that isn't a real bot used to silently "succeed"
  (the underlying session-creation path auto-creates a session under any
  name and answers anyway, no actual bot behind it) instead of failing
  -- a typo'd or hallucinated name looked exactly like a real reply.
- `create_bot`'s tool result no longer reports a "provider" field -- a
  known cosmetic staleness right after creation (the bot's actual
  routing is correct regardless), not worth surfacing to the user as if
  it were meaningful, reliable information.
- `backend/resilience.py`: the chat-retry logic's failure checks
  (server-error rollover, corrupted-reply retry) are now independent,
  pluggable, individually unit-tested functions instead of inline
  conditionals in `engine.py`'s `send_to_bot()`.
- `backend/supervisor_mcp.py` (the `bot-supervisor` MCP tool server) now
  actually runs -- it existed in the repo but nothing started it. Wired
  into `entrypoint.sh` as a third in-container process and registered in
  the bootstrapped `config.yaml`'s `mcp_servers`.
- `tests/test_resilience.py`: direct unit coverage for the resilience
  checks, no mocking required.
- `create_bot` MCP tool: the only correct way for a bot to create another
  bot, wrapping the real `/bots` registry endpoint. Replaces a real
  incident where a bot with no purpose-built tool for this fell back to
  exploring the filesystem and running CLI commands, took ~10 minutes,
  and left the new bot broken (no explicit model/provider).
- `backend/persona.py`: a real zBots-branded default persona. Every bot
  previously inherited the underlying engine's own stock identity
  verbatim ("You are Hermes Agent, an intelligent AI assistant created
  by Nous Research") -- found live, from the same incident above.
- `delegate_task` MCP tool: fire-and-forget task handoff between bots --
  a bot delegates work to another bot without blocking on it, and the
  result arrives as a new message in its own session once the worker
  finishes. See `docs/design/supervisor-delegation.md`.
- Root nginx timeout for `/bots-api/` raised from nginx's 60s default to
  300s -- a real, correctly-answered tool-use turn was hitting the
  default and 504ing even though the backend kept working and produced
  the right answer.

### Fixed
- The embedded chat engine cached a profile's resolved persona/model at
  first use and never re-read it -- a soul/model/description edit made
  through the dashboard API silently had no effect on chat until the
  whole container restarted. `engine.invalidate_adapter()` now resets
  that cache after any such edit, verified live to take effect on the
  very next message.
- MCP tool discovery was a silent casualty of the embedded engine
  skipping `runner.start()` (needed to avoid ITS other side effects) --
  a bot configured with an MCP server never actually connected to it.
  Fixed by calling `discover_mcp_tools()` directly, the same standalone
  function upstream's own startup calls.
- The branding-safety persona instruction alone wasn't reliable -- a
  direct "who made you" question still leaked "Hermes"/"Nous Research"
  in 2 of 3 identical requests, even with the correct persona verified
  in effect. `persona.redact_branding_leaks()` is now a deterministic
  scrub applied to every reply, since a probabilistic model choosing to
  disclose something it was told not to isn't fixable by rewording the
  instruction alone.
- Any bot other than "default" had a genuinely empty message history in
  the UI regardless of how much it had actually chatted -- confirmed
  live: a real, successful conversation with a second bot showed nothing
  when viewing that bot directly. Root cause: `get_bot_messages()` was
  wrapping its per-session message fetch in `_profile_scope`, which
  causes the underlying engine's message-read handler to return zero
  results for any non-default profile, even for a session it provably
  owns (confirmed: the identical call, unscoped, returns the real
  messages correctly). Session creation and listing were never affected
  -- only reading a non-default bot's own messages back. Dropped the
  scope for that one call; a session is already looked up by its own
  globally unique id, so nothing about correctness depends on it.
- `title_generation` disabled in the bootstrapped config -- the
  engine's own auto-titling rewrites a session's title from its opening
  message, which fights the title pattern zBots' session-family
  tracking (rollover, `get_bot_messages`) depends on to recognize a
  bot's own sessions.
- `create_bot`: a bot asked to create another bot "named X" would
  sometimes invent a different display title on its own initiative
  (e.g. asked for "tt", titled it "Travel Planner") -- title now
  defaults to the given name unless the caller explicitly passes a
  different one.

## [0.1.0] - 2026-08-23

First tagged release. zBots as a standalone product: its own container,
its own branding, no dependency on a separately-deployed Hermes instance.

### Added
- Standalone Docker image: nginx in front, FastAPI backend, chat/sessions
  running in-process against a vendored, pinned `hermes-agent` snapshot
  (no upstream git remote -- see `vendor/VENDORED_COMMIT.md`).
- MeanMachines Technologies branding throughout; zero Hermes/Nous Research
  mentions in the product UI, README, or marketing copy. MIT compliance
  handled separately via `THIRD_PARTY_LICENSES.md` and `docs/CREDITS.md`.
- One-click MCP integration catalog, matching the real Hermes app.
- `bot-supervisor` MCP tools (`list_bots`, `message_bot`, `get_bot_status`)
  for cross-bot supervision.
- Live streaming bot replies instead of blocking for the full response.
- Session-locking a bot's provider/model at creation time, fixing new bots
  getting silently coerced to a broken provider and the roster showing the
  wrong provider label.
- Group chat editing (rename, member changes).
- `/ready` and `/version` health probes for orchestrator checks.
- Root domain now redirects to the app (`/bots/`) instead of leaking
  nginx's stock welcome page; unmatched paths return a clean 404.
- CI: the test suite now runs on every push/PR.

### Fixed
- The vendored engine's own build deliberately blocks a regular
  (non-editable) `pip install` -- the Dockerfile now installs it with `-e`,
  matching hermes-agent's own documented workaround.
- The container healthcheck silently failed with neither `curl` nor `wget`
  available in the base image.
- The Hermes dashboard backend (roster, profile CRUD, MCP servers, skills,
  env vars, cron, webhooks, files, config) wasn't started at all in the
  standalone container -- only chat worked. Now runs as a second
  in-container process.
- That backend was bound to loopback, which silently skips its real
  password-login auth gate in favor of a different, unsupported path --
  every dashboard call 401'd. Now bound to `0.0.0.0` (still
  container-internal only; nothing publishes that port to the host).
- `aiohttp` was never installed despite the embedded chat path depending
  on it directly (`aiohttp.test_utils.make_mocked_request`).
- The bootstrapped provider config's shape didn't match what the real
  resolver expects, so every chat request failed with "No inference
  provider configured" despite the roster showing a model correctly.
- Cross-bot message bleed during streaming (DOM writes weren't gated on
  the currently active chat).
- Directory browsing and file reads 500'd for every path beyond root due
  to a keyword-argument collision in the dashboard client helper.

### Changed
- Chat/sessions now run in-process against the vendored engine instead of
  over loopback HTTP to a separately-running gateway. Profile/config CRUD
  stays on HTTP against the in-container dashboard backend -- see
  `backend/engine.py`'s module docstring for why that split exists.

[0.1.0]: https://github.com/meanmachines/zbots/releases/tag/v0.1.0
