# Changelog

All notable changes to zBots are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions are
tagged on `main`.

## [Unreleased]

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
