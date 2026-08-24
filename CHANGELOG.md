# Changelog

All notable changes to zBots are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions are
tagged on `main`.

## [Unreleased]

### Added
- Bare URLs in chat now render as real clickable links -- the markdown
  renderer previously only linkified `[label](url)` syntax, so a bot
  just typing a plain `https://...` URL (very common mid-setup-flow,
  e.g. "here's the auth link: https://...") rendered as dead, unstyled
  text the user could only select and copy, not click.
- OAuth/consent-flow authorization links (Google, LinkedIn, Microsoft,
  Slack, GitHub) render as a distinct "Connect" button instead of
  blending into the setup prose as one more inline link -- the one link
  that actually matters is the one the user needs to click. Matched by
  the real, stable OAuth authorization endpoint host (confirmed against
  hermes-agent's own google-workspace skill, the mechanism that
  generates these links today), not by guessing at path keywords.
  - Fixed a real, pre-existing double-escaping bug found getting this
    right: a link's URL is already HTML-escaped by the time it reaches
    the code building its `href` (mdInline's own leading `mdEscape(text)`
    pass runs first), so escaping it again turned a query string's `&`
    into `&amp;amp;` -- which a real OAuth authorize URL (exactly the
    case this feature most needs to work) almost always has. Existed for
    `[label](url)` links too, just never noticed since it silently
    produced a still-technically-clickable (if cosmetically broken) link
    rather than an obviously wrong one.

### Fixed
- Real regression found live, right after the chronological preview-card
  ordering fix below shipped: making `renderMessages` interleave awaited
  network fetches directly into its per-row render loop meant one call
  could take multiple real seconds, and the poll timer firing again
  mid-way (every 5s, unconditionally) started a SECOND overlapping render
  that wiped the pane out from under the first -- reported live as chat
  messages "constantly coming and going." Fixed by making the base
  message render fully synchronous again (no awaits in that loop at all)
  and moving preview-card insertion to a separately-guarded async step: a
  `renderGeneration` counter is bumped once per render call, and every
  DOM mutation in the async step checks it first, so a render superseded
  by a newer poll silently stops touching a pane it no longer owns
  instead of corrupting it. Confirmed live: message count in the pane
  now holds rock-steady across repeated poll cycles instead of swinging
  wildly (44 -> 5 -> 38 -> 44 -> ...).
  - A second, smaller version of the same flicker surfaced fixing the
    first: the preview-cache TTL (4s) was shorter than the poll interval
    (5s), so every single poll forced a real network re-fetch for every
    card, and the instantly-rendered base message list beat the cards
    back into view by a beat -- visible as cards vanishing and
    reappearing every 5 seconds. Fixed by raising the TTL to 15s,
    comfortably above one poll period, so a normal poll hits cache
    (same paint frame, no visible gap) and a genuinely regenerated file
    still surfaces within one TTL window rather than indefinitely.

### Added
- A new `RESPONSE_STYLE` guardrail: when a bot revises a file it already
  generated for the user (a webpage, image, document) based on feedback,
  it now saves the new version under a NEW filename instead of
  overwriting the original. Reported live as "generations aren't
  working, I still see the same page" -- the bot WAS actually producing
  updated content each time, but silently overwriting the same path, so
  a stale client-side preview cache (see the TTL fix above) made every
  iteration look like nothing had changed. Versioning the file is the
  real fix (the user can always go back and compare an earlier version,
  not just trust a timestamp), with the cache TTL as a second line of
  defense for whatever still shares a path.

### Fixed
- Preview cards were rendered in one batch after the whole message loop,
  so a file generated early in a long conversation still landed at the
  very bottom of the chat, below messages sent long after it -- newest-
  message-last (the one thing a chat pane must always get right) was
  broken. Real bug reported live. Fixed by having `findPreviewPathsInHistory`
  record each path's first-occurrence timestamp and `renderMessages`
  interleave each card right after the message it chronologically
  belongs to, awaiting each insertion in order (not fire-and-forget --
  concurrent unawaited fetches can resolve out of order and undo the fix).
  Confirmed live against a real conversation with several generated
  files at different points: each card now lands exactly where it was
  generated, and the conversation still ends on the latest real message.
- `previewCache` never expired, so a bot regenerating the SAME file path
  (e.g. "change the color to purple" on a landing page it already built)
  kept showing whatever content was fetched the FIRST time, forever --
  reported live as "generations aren't working, I still see the same
  page." Fixed with a short (4s) TTL instead of session-permanent
  caching: still dedupes repeat hits within one render pass, but the
  next poll always re-fetches, so a genuinely-changed file is never more
  than one poll cycle stale.
- The preview panel's "Open full size" link silently did nothing when
  clicked -- real bug found live: it pointed at the file's `data:` URI
  directly with `target="_blank"`, and Chrome (and other modern browsers)
  refuse to open a `data:` URI as a new top-level tab at all, a
  deliberate anti-phishing restriction (a `data:` page has no real
  address-bar identity to show). Fixed by converting the data URI to a
  same-origin `blob:` URL client-side before handing it to the link (and
  to the iframe/img themselves, for the same robustness -- a blob avoids
  re-parsing a megabytes-long base64 string as a URL, which some browsers
  cap). The previous blob is revoked on every new preview and on close so
  it doesn't pin memory for the page's whole lifetime.

### Added
- A download button next to "Open full size" in the preview panel,
  reusing the same blob URL with a real filename via the `download`
  attribute -- works for both pages and images.
- Live preview of anything a bot generates -- HTML pages, images, icons,
  SVGs -- in a dedicated side panel (`#preview-pane`), instead of a file
  the user has to already know exists and go find on the Files page.
  Tool-agnostic by construction: scans every tool call's args, and
  separately the bot's own reply text, for a string that looks like a
  previewable file path (`.html`/`.htm`/`.png`/`.jpg`/`.jpeg`/`.gif`/
  `.svg`/`.webp`/`.ico`), rather than hardcoding one tool name -- any tool
  that touches a file path works automatically, no allowlist to maintain.
  Also understands hermes-agent's own real `MEDIA:<path>` tag convention
  (`api_server.py`'s `_resolve_media_to_data_urls`) -- confirmed live that
  tag only ever gets resolved to an inline image in the live SSE event at
  generation time, never in what actually gets persisted to message
  history, so a reopened conversation showed the literal unresolved
  "MEDIA:/root/foo.png" text with no image at all; this pipeline picks up
  the same tag and resolves it independently. No new backend plumbing
  needed either way -- `GET /files/read` already returns a `data_url`
  (`data:<mime>;base64,...`) for any file type, so the exact same fetch
  serves both pages and images. Pages render in a sandboxed iframe
  (`allow-scripts allow-forms`, deliberately no `allow-same-origin`/
  `allow-popups`/`allow-top-navigation` -- a `data:` URI iframe is
  already an opaque origin the parent can't be reached from, this is
  defense in depth against a generated page's own script trying to
  navigate the tab or spawn windows); images render in a plain `<img>`
  for correct aspect-ratio sizing and native zoom/save behavior. A small
  clickable chip in the chat (filename + type) opens the panel -- started
  as a rendered-inline card, but real feedback live was that a full page
  squeezed into chat-bubble width was unreadable and its own "open full
  size" link unusable at that size, so the panel replaced it entirely.
  Detection runs against the bot's real persisted history (not just the
  live SSE stream), so a preview survives a poll reload or reopening an
  older conversation, not just the turn that created it -- confirmed live
  against several images and a landing page a bot had already built
  earlier, with zero new messages sent. Cached client-side per path after
  the first fetch so the 5s poll doesn't re-fetch an unchanged file's
  base64 content on every tick.
  - Three real bugs found and fixed getting this to actually work right:
    `.preview-card` (one class) lost every property it shared with
    `.msg.bot` (two classes, matching specificity beats source order) --
    fixed by compounding onto `.msg.preview-card` the way `.msg.user`/
    `.msg.bot` already do it themselves. `overflow: hidden` on a flex
    item (`#messages-pane` is a column flex container) resolves that
    item's automatic min-height to 0 per the flexbox spec's own
    min-size-auto rule, collapsing the card to zero height around a
    correctly-sized iframe -- fixed with `flex-shrink: 0`. And widening
    the path-matching regex to cover image extensions also started
    matching into remote image URLs mentioned elsewhere in an unrelated
    conversation (a weather-icon CDN) and generic example paths
    ("/path/to/image.png") -- a shared match-count budget across a whole
    history scan meant those false positives could crowd out the real,
    recent file before it was ever reached; fixed by requiring an actual
    path separator and excluding ":" from the matchable characters (so
    it can never match into an http(s):// scheme), plus raising the
    history-scan budget well above the live-stream one.

### Fixed
- `get_bot_messages()` merges every session in a bot's rollover family back
  into one timeline (by design -- so a mid-conversation rollover doesn't
  make earlier messages disappear), but never accounted for the fact that
  a rollover resends the user's own message into the fresh session. Real
  bug found live, reproduced with a real multi-tool-call turn: the user's
  own prompt visibly appeared twice back-to-back in the chat (three times
  for a double rollover) even though it was only typed once. Confirmed by
  reading two rollover sessions' own rows directly: both started with the
  literal same "user" text. Fixed with `_dedupe_rollover_replay()`,
  collapsing a run of consecutive same-text user turns into one -- safe
  unconditionally, since a genuine accidental double-send by the user
  reads identically either way. Pinned with 3 new tests.
- `stream_to_bot()`'s rollover forwarded every frame from attempt 1 live,
  including its own `assistant.completed` -- safe under the OLD frontend
  (only ever rendered `assistant.delta`), but broke the moment the
  collapsible-thinking-panel redesign made `assistant.completed` THE
  answer. Real bug reported live right after that shipped: on any turn
  needing a rollover (e.g. the stale-model-lock case), the user briefly
  saw attempt 1's own answer (sometimes a raw rejection) render, then
  attempt 2's real one replace it right after -- reported as the reply
  "appearing and disappearing" and the true answer "not showing up"
  (attempt 1's content was never persisted, so it vanished on the next
  reload). Fix: progress frames (`tool.*`, `assistant.delta`) still
  forward live, but anything that could be mistaken for a final answer
  (`assistant.completed`, `run.completed`, `error`) is held back until
  the whole attempt is known not to need a retry -- discarded entirely if
  it does. Pinned with 3 new tests in `test_engine_streaming.py` driving
  the real async generator against a mocked attempt sequence.

### Added
- OpenRouter as a selectable provider in the model switcher (the dropdown
  under a bot's name) and the Models page, alongside every other
  provider, so it can be switched to on demand instead of sitting unused.
  OpenRouter is one of hermes-agent's own native routing modes (resolved
  through `OPENROUTER_API_KEY` at call time), not a custom OpenAI-
  compatible endpoint -- adding it the normal "Add provider" way is
  blocked on purpose (`_reserved_provider_ids`), since that form's own
  base_url/api_key fields would be silently ignored for this name. Wired
  in as a `providers.openrouter` entry carrying only a curated models
  list (Claude Sonnet 5, Claude Opus 5, GPT-5.5, Gemini 2.5 Pro,
  DeepSeek R1, Grok 4.5) and no credentials of its own, so real auth
  still goes through the native env-var path. `entrypoint.sh`'s bootstrap
  now writes this same block on a fresh deploy, but only when
  `OPENROUTER_API_KEY` is actually set -- offering a choice that would
  just fail when picked helps no one. Note: `OPENROUTER_API_KEY` is not
  currently set on zbots-dev, so the option is selectable but won't
  complete a real request until a key is added.

### Changed
- Replaced the tool-status indicator's write-then-erase bubble pattern
  with a collapsible "thinking" panel. Real bug found live right after
  that indicator shipped: a turn with several tool calls repeatedly
  created a bubble on `assistant.delta`, then deleted it whenever
  `tool.started`/`tool.progress` followed (needed since narration text
  can stream before a tool call, see that fix's own comment) -- with
  multiple tool cycles in one turn that's a repeated write-then-erase of
  a bubble-shaped thing, which read as flickering. Fix restructures
  around a fact confirmed by reading the real handler
  (`gateway/platforms/api_server.py`): `assistant.completed`'s `content`
  field always carries the full, authoritative final reply, regardless
  of how many delta/tool cycles preceded it -- so it's now the ONLY
  source ever rendered as the visible answer, appended exactly once.
  Everything else that happens mid-turn (tool calls, reasoning ticks)
  goes into a `<details>`-based panel, collapsed by default with a
  live-updating one-line summary ("Checking bots…", "Responding…"),
  expandable to a full step log -- same "thought for a bit" affordance
  as a desktop chat app. Verified live with a real multi-tool-call turn
  (`list_routines` + `message_bot` + `list_bots` + `message_bot` again,
  8 logged steps): the visible message list stayed byte-identical
  through the entire in-progress phase, with the one real answer
  appearing once at the end.
- `persona.py`'s `RESPONSE_STYLE` now also asks for short, conversational
  replies -- a couple of sentences for most things -- with bullet points
  or short headers for anything that genuinely has several details to
  convey, instead of long unstructured paragraphs. Rolled out the same
  way the rest of `RESPONSE_STYLE` reaches existing bots (it's appended
  automatically the next time a soul is fetched through
  `with_branding_safety`).

### Fixed
- `_process_sse_frame`'s stale-model-lock detection only inspected
  `assistant.delta` frames -- real bug found live, immediately after a
  real provider switch: the same underlying failure sometimes streams
  zero real tokens at all, delivering its whole rejection in one
  `assistant.completed` frame (`content`, not `delta`) instead, which the
  delta-only check couldn't see, so it reached a real session unflagged.
  Now checks both event shapes. Verified against the actual affected
  session: the same request that previously surfaced the raw error now
  gets caught and retried automatically, landing on a real answer.

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
