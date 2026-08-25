# Changelog

All notable changes to zBots are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions are
tagged on `main`.

## [Unreleased]

### Added
- Multi-hour autonomous "developer" bot sessions with live steering,
  matching (and improving on) a real 21.5-hour hermes-agent desktop `coder`
  session read directly off the user's own machine (`state.db`, found via
  the Electron app's own `backend-ownership.json`) -- see the full write-up
  in `stateful-prancing-gadget.md`'s plan for the investigation. Key
  finding: hermes-agent's own agent loop is already unlimited by default
  (`hermes_cli/config.py`'s `resolve_turn_limit` -- an absent `max_turns`
  resolves to `TURN_LIMIT_UNLIMITED`) and every real `write_file` call
  already self-verifies (`"verified"`/`"lint"` in its own return value) --
  the gap was entirely in zBots' own wrapper, not a missing hermes feature.
  - **Real bug fixed**: `engine.py`'s `_call_handler_http` hard-capped
    every plain (non-streaming) chat call at 120 seconds -- what
    `send_to_bot`/`supervisor_mcp.py`'s `message_bot`/`delegate_task`/
    routine deliveries all use. A real multi-hour agentic turn got killed
    mid-run, converted to a 500, and handed to a rollover that threw away
    the in-progress session for a fresh one with only a text recap --
    actively destructive to a marathon build. Raised to a 6-hour ceiling
    (still finite, as a last-resort safety net for a truly dead worker --
    hermes' own `tool_loop_guardrails` is the correct first line of
    defense against a genuinely wedged loop, not this outer timeout). The
    streaming path already had no such cap.
  - **Real bug fixed**: `create_bot()` only PUT a soul when the caller
    explicitly supplied one (`if body.soul: ...`) -- every bot created
    with an empty soul (the common case) fell straight back to
    hermes-agent's own raw stock persona ("You are Hermes Agent... created
    by Nous Research"), skipping `persona.py`'s branding guardrails
    entirely. Confirmed live: 6 of 9 real bots on zbots-dev had exactly
    this, including two created earlier the same session with no soul
    set. `persona.with_branding_safety(body.soul)` is now called
    unconditionally (it already handled "empty -> DEFAULT_SOUL" correctly
    on its own).
  - New live steering: `POST /bots/{name}/steer` redirects a bot while
    it's still actively working, instead of queuing a new turn for after
    it finishes -- the real gap behind the observation that the real
    desktop session wasn't fully hands-off (a real, pending mid-build
    steering message was found live in `interrupted_turns.json` against
    the exact same 21.5-hour session). Uses hermes-agent's own native
    `POST /v1/runs/{run_id}/steer` ("inject guidance into a running
    agent") -- not a new mechanism to build, just never wired up: the
    same `_handle_session_chat` endpoint zBots' streaming path already
    calls creates this same `run_id` on every call and stamps it onto
    every streamed event's own payload, so `engine.py`'s `stream_to_bot`
    now captures it live (from whichever frame carries it first, no need
    to touch the narrower assistant.delta/completed-only frame parser)
    into the same `session_state` dict `main.py` already holds a live
    reference to for the duration of that stream. Frontend: sending a new
    message while that exact bot is still streaming now calls `/steer`
    instead of being silently dropped (the previous behavior -- the
    composer's own send guard just returned early with no feedback at
    all); once the bot goes quiet, the composer behaves exactly as
    before. HTTP-transport only (no embedded-transport equivalent -- that
    path has no live, concurrently-reachable agent object a second call
    could reach into).
  - `developer`-category bots (`coder` first) now get the same
    `tool_loop_guardrails`/`compression`/`session_reset` tuning the real,
    proven desktop profile runs with, applied via the SAME per-profile
    config-write mechanism `_sync_profile_provider` already relies on
    live (`PUT /api/config` with a `profile=` query param -- not a new
    mechanism, just reused). `agent.max_turns` deliberately left unset --
    hermes' own default is already unlimited, more room than the real
    profile's own deliberate 500-turn ceiling.
  - `persona.py`'s coding-handoff guidance no longer claims `coder` "runs
    real external coding-agent CLIs (OpenCode, Qwen Code)" -- the real,
    proven 21.5-hour session never used one; it's hermes' own native
    `terminal`/`write_file`/`execute_code`/`todo` tools throughout, on the
    same model zBots' own `coder` already runs. Updated to describe that
    directly instead.
- Real browser push notifications for routine/delegated-task deliveries
  ("browser push notification for a hydration reminder... works even if
  the zBots tab isn't focused" -- requested live). Checked hermes-agent's
  own vendored source first, per instruction: the desktop app's own
  notification mechanism is Electron's native `Notification` module,
  driven by IPC from the renderer -- no web-based equivalent, and no
  VAPID/service-worker/Web-Push code anywhere in `hermes_cli`'s own
  Python side either. Nothing to reuse; built as a new capability.
  - New `backend/push.py`: real Web Push (RFC 8030 + VAPID, via
    `pywebpush`), not a same-tab `Notification()` call. VAPID key pair
    generated once via `py_vapid`'s own `Vapid.from_file()` (persists to
    the volume, never regenerated -- a changed key silently invalidates
    every existing browser subscription) . Subscriptions stored in their
    own small JSON registry, same convention as `bot_processes.py`'s own
    port/worker registry; a subscription the push service itself reports
    gone (404/410) gets pruned automatically on next send. Covered by
    `tests/test_push.py` (12 tests) -- the real network send is mocked,
    key generation/persistence and subscription CRUD are exercised for
    real (cheap, local, no network).
  - New endpoints: `GET /push/vapid-public-key`, `POST /push/subscribe`,
    `POST /push/unsubscribe`.
  - `SendMessage.notify` (default `False`): when `true`, a successful
    `POST /bots/{name}/messages` fires a push notification in the
    background (`_fire_and_forget`, same convention as
    `supervisor_mcp.py`'s own delegated-task pattern -- a slow/failing
    push send must never add latency to, or fail, the chat reply itself).
    The interactive UI's own calls never set it (the user is already
    watching the reply arrive live); `supervisor_mcp.py`'s `message_bot`
    tool and the result-delivery leg of `delegate_task` now do, since
    those are asynchronous deliveries the user isn't necessarily watching
    -- exactly the routine/reminder-delivery case that prompted this.
  - Frontend: new `frontend/sw.js` service worker (`push`/
    `notificationclick` handlers -- clicking a notification focuses an
    already-open zBots tab instead of always opening a new one). New
    opt-in bell button in the sidebar header (`common.js`'s
    `enablePushNotifications`/`disablePushNotifications`) -- deliberately
    NOT an auto-prompt on page load (an unsolicited permission prompt on
    first visit is a real anti-pattern Chrome's own abuse heuristics can
    penalize, and this app has no way to know if a given load is a first
    visit).
  - Two real bugs found and fixed live during end-to-end verification
    (subscribed for real, sent a real test push, heard the notification
    sound arrive but with no visible title/body -- traced with a real
    minimal repro against pywebpush directly rather than guessed at):
    - `_send_one` passed `vapid.private_pem().decode()` (a PEM STRING) as
      `vapid_private_key` -- pywebpush's own `webpush()` only accepts a
      real `Vapid` instance, a file path, or its own `Vapid.from_string()`
      encoding (confirmed by reading pywebpush's own source), not
      arbitrary PEM text. The PEM string fell through to
      `Vapid.from_string()` and raised a bare `ValueError` ("Could not
      deserialize key data... ASN.1 parsing error") *outside*
      `WebPushException` entirely -- not even caught by `_send_one`'s own
      except clause, silently killing every send via
      `send_push_notification`'s outer best-effort try/except (by
      design, for network failures -- not for a bug that fails 100% of
      the time). Fixed by passing the real `Vapid` instance directly,
      which pywebpush uses as-is with no string re-parsing at all.
    - Once that was fixed, sends succeeded with the wrong-key bug gone
      but still returned a bare `400 Bad Request` (empty body, no
      detail) from WNS (Windows' own push endpoint) specifically.
      `pywebpush.webpush()`'s own default is `ttl=0`; RFC 8030 defines
      that as "deliver now or drop, never queue" -- WNS rejects it
      outright rather than honoring it. Fixed with a real, positive
      default TTL (`PUSH_TTL_SECONDS`, 1 hour) -- confirmed live,
      identical payload/key, only the TTL changed, real 201 from WNS and
      a real notification with visible title/body arrived.
    - Covered by new tests in `test_push.py` (`_send_one` gets a real
      `Vapid` instance, not a string; `_send_one` calls `pywebpush.webpush`
      with a positive `ttl`).
- Bot lifecycle categories: `chore | task | developer | supervisor |
  general` (requested live -- "classify bots into the ones that does daily
  chores or ones which does small tasks... one that needs to do multi turn
  in auto mode for days... and then supervisor or quality control bots that
  can keep eye on other bots' work"). Every bot lands in exactly one
  category, stored in zBots' own `state.json` (not hermes-agent's native
  profile config -- this is zBots' own bookkeeping, same convention as
  `titles`/`locked_models`), with `general` as the implicit default for
  every bot that existed before this.
  - `_infer_bot_category()`: classifies a new bot from its own description
    via one real chat turn on a temporary, throwaway session on `default`
    (never `default`'s own visible chat thread) -- an LLM classification
    call, not string matching. Falls back to `general` on an empty
    description, an unparseable reply, or any failure; the temporary
    session is deleted afterward, best-effort. An explicit `category` on
    `POST /bots` skips inference entirely. `PATCH /bots/{name}` accepts a
    manual override at any time, per the user's own instruction ("the user
    can manually change its type as well later if required") -- no
    re-inference call, a direct statement is taken as-is.
  - `_keep_warm_bots()` is now category-aware: `chore`, `developer`, and
    `supervisor` bots stay warm unconditionally (recurring/long-running/
    always-watching work all need to be instantly reachable), same as
    `default` and any bot with an enabled routine (kept as an additional
    signal, not replaced). `task` and `general` bots stay on-demand,
    30-minute idle-reaped as before.
  - `task` bots get a fresh session on every call -- a one-off request has
    no business remembering a previous unrelated ask. The old session is
    never deleted, same convention as a rollover -- history stays real and
    readable, a `task` bot just never reads its own past history back into
    context.
    - Real bug found live during end-to-end verification: the first cut of
      this just passed `active_session_id=None` to `send_to_bot`/
      `stream_to_bot`, on the assumption that `_ensure_bot_chat_session`'s
      "no active session id" path always creates a new session. It
      doesn't -- once ANY session already exists for the profile, that
      path's own fallback reuses `all_sessions[-1]` (a title-family
      search meant for "state was lost, recover the real session"),
      regardless of what active_session_id was. Confirmed live: told a
      task bot a secret in message 1, then asked "what was the secret
      code I just gave you?" in message 2 -- it answered correctly, which
      it should never have been able to do. Fixed with an explicit
      `force_new` parameter on `_ensure_bot_chat_session` (and
      `force_new_session` threaded through `send_to_bot`/`stream_to_bot`)
      that skips the reuse fallback unconditionally and starts a genuinely
      new session, still numbered into the bot's own title family (so it
      still merges into one continuous visible thread -- only the MODEL's
      own context is isolated, not the user-visible history). Re-verified
      live after the fix: the same two-message exchange no longer recalls
      the secret. Covered by new `test_engine_sessions.py` (6 tests).
  - New `POST /bots/{name}/wake`: opportunistic pre-warm, best-effort
    (never a visible failure) -- fired by the frontend the moment a bot's
    chat is opened (`selectBot()`), in parallel with loading its message
    history, so the worker is already starting while the user is still
    reading/typing instead of only on first send.
  - Frontend: category select in the create/edit bot modal (Advanced
    section) -- "Auto-detect from description" (default, triggers
    inference) plus the four named categories plus General. No new roster
    indicator -- the roster row already carries an avatar, title, time,
    preview, and an active/offline dot; a category dot would be exactly
    the kind of badge-creep the UI has deliberately stayed away from.
  - Supervisor/QC bots are a configuration pattern, not new mechanism: a
    `supervisor`-category bot paired with a routine whose prompt calls the
    existing `supervisor_mcp.py` tools (`list_bots`, `get_bot_status`,
    `message_bot`) across the roster and reports findings -- delivered
    through the same routine-delivery path every other routine already
    uses, so it gets a real push notification automatically. Documented in
    `docs/supervisor-bots.md` (persona + sample routine), not built as a
    separate capability.
  - Covered by new tests in `test_backend.py` (category
    inference/validation/parsing/fallback, category-aware keep-warm,
    `task`-bot fresh-session behavior, the `/wake` endpoint).

### Fixed
- Branding leaks in zBots' own frontend chrome (labels, error banners, a
  confirm dialog) and one backend API error message, found via a full
  audit of every "hermes" mention in zBots' own (non-vendored) code --
  bot chat replies were already covered (`persona.py`'s system-prompt
  instruction plus a deterministic `redact_branding_leaks` regex scrub),
  but the UI zBots itself built around that had never gotten the same
  pass: the Connectors page said "the same channels the Hermes desktop
  app supports", the MCP catalog modal said "the same catalog the Hermes
  desktop app's... uses", the System page's restart confirm dialog read
  "Restart the Hermes gateway?", the Connectors page's offline-gateway
  banner fell back to telling an admin to run `hermes gateway run` (a raw
  CLI command a zBots admin has no way to actually invoke), and a real
  dashboard-auth failure path returned `Could not authenticate to the
  Hermes dashboard: HTTP {status}` as its error detail. All five
  rewritten to carry no hermes-agent branding; internal code comments and
  the required upstream attribution in `README.md`/
  `THIRD_PARTY_LICENSES.md`/`docs/CREDITS.md` were left alone (accurate
  technical documentation and license compliance, not user-facing leaks).
  - A much bigger version of the same bug, found immediately after live-
    verifying the five above: `/connectors`, `/skills`, `/mcp/catalog`, and
    `/plugins` are all thin `dash_get` proxies of hermes-agent's own native
    dashboard API (same convention as everywhere else in `main.py`), and
    hermes-agent's own response text for these -- `description`/`prompt`/
    `help`/`name` fields -- carries its own branding verbatim. Confirmed
    live against the deployed app: 27 of 33 connector platforms ("Connect
    Hermes to Discord DMs...", "Use Hermes from Slack via Socket Mode...")
    and 46 separate mentions inside the MCP catalog's own per-integration
    setup text alone. `redact_branding_leaks` only ever ran on a bot's own
    chat reply (`engine.py`'s `send_to_bot`) -- nothing scrubbed these
    admin-facing config-proxy responses. Fixed with a new
    `persona.scrub_branding_deep()`: recursively applies the same regex
    scrub to every string in a JSON-shaped dict/list, wired into all four
    endpoints. `docs_url` fields are deliberately excluded -- they're real,
    working links to hermes-agent's own setup docs (e.g. how to get a
    Discord bot token); scrubbing "hermes-agent" out of
    "hermes-agent.nousresearch.com" would silently break the domain rather
    than remove a leak. Covered by 6 new tests in `test_persona.py` and 5
    endpoint-level tests in `test_backend.py`.
- Real bug found live: a resilience-triggered rollover (see the
  `gateway.multiplex_profiles`/provider-scoping fixes above for the two
  other real bugs this session's rollover logic already recovers from)
  swaps in a genuinely fresh session with zero conversational memory, even
  though `get_bot_messages`' own merged-history view makes the whole
  session family look like one continuous thread. Confirmed live: a
  direct, short follow-up ("it should remind every 5 minute") landed on a
  freshly-rolled-over session with no idea what "it" referred to, and the
  model answered "I'm not sure what you're referring to" -- correct given
  what it could actually see, useless to the person who'd just been asked
  a direct question one message earlier. Traced with real evidence (two
  distinct session ids ~107 seconds apart, the rolled-over session's own
  message history starting cold) rather than assumed.
  - Fixed by borrowing hermes-agent's own PATTERN for a related problem
    (`gateway/run.py`'s `_pending_model_notes` -- prepends a short note to
    the next outgoing message so the model knows about a model switch
    that just happened) rather than the mechanism itself (an internal
    contextvar zBots' session-level rollover has no access to, since
    rollover is a zBots-level workaround sitting a layer above the
    engine's own session handling). New `_context_bridge_note()` in
    `engine.py`: pulls the old session's last few turns and prepends them
    as plain recap text ahead of the retried message -- deliberately no
    extra LLM call to summarize, just the raw recent turns verbatim
    (truncated). Wired into both `send_to_bot`'s and `stream_to_bot`'s
    rollover paths.
  - Real follow-on bug caught before it shipped: prepending the recap
    changes what's persisted as the user's own retried message, which
    broke `_dedupe_rollover_replay`'s exact-text-equality check (built
    earlier this session for the plain, noteless replay case) -- the
    note-prefixed retry no longer matched the original message exactly,
    so it stopped collapsing to one and the user's own prompt would have
    reappeared twice in the merged view again. Fixed by matching on
    "one text ends with the other" instead of exact equality, which
    correctly collapses both the noteless and note-prefixed replay
    shapes, keeping the clean (noteless) copy as the one that survives.
  - Also clarified, not fixed (turned out already correct): the
    delegation-confirmation-routing concern raised alongside this turned
    out to already work as expected -- reading the actual session history
    confirmed the delegating bot's own "Done, I set up..." confirmation
    really did land back in its own chat; the described symptom was this
    same rollover bug on the very next turn, not a routing problem.
  - Follow-on cosmetic bug found live right after deploying the fix
    above: a bridged retry becomes the fresh session's own opening
    message, and hermes-agent's own native `preview` field (the roster's
    "last message" snippet, set from a session's first stored message,
    confirmed live by reading real session objects -- a cron session's
    own preview is literally its opening system prompt) showed the
    internal recap note instead of the user's actual message for any bot
    that had just rolled over. `get_bot_messages`' own merged-chat view
    already hid this correctly via the dedup fix above; `preview` reads
    straight off the native session object, bypassing that entirely.
    Fixed with `engine.strip_context_bridge_note()`, used by
    `main.py`'s `get_bot_activity()`.
    - That fix's own first version (marker-search: find
      `_CONTEXT_BRIDGE_END_MARKER`, return what comes after it) shipped
      broken -- verified live on zbots-dev, the roster still showed the
      raw note. Root cause: `preview` turns out to already be truncated
      by the native engine itself, well short of even the recap's own
      last line, so the end marker being searched for had usually
      already been cut off before this function ever saw the text --
      real user text isn't recoverable from a pre-truncated preview at
      all. Fixed properly by detecting the note's own fixed OPENING
      words instead (survives truncation) and returning a plain,
      honest placeholder rather than trying to reconstruct text that's
      actually gone.

### Changed
- Chat backend architecture: replaced the shared, multiplexed `hermes
  gateway run` process (one process serving every bot's chat via
  `/p/<profile>/` URL-prefix scoping) with a genuinely separate,
  dedicated worker process per bot -- every bot, including `default`, now
  runs its own unscoped, single-profile `gateway.run.start_gateway()`
  instance. This is the real fix for the class of credential/config-
  scoping bug this session found and patched twice (see the two entries
  below this one): request-time scoping inside one shared process was an
  ongoing surface for exactly this kind of bug, and removing the sharing
  removes the whole class instead of patching instances one at a time.
  - New `backend/bot_worker.py` -- the actual per-bot process entry
    point. Sets `HERMES_HOME`/`API_SERVER_PORT` for that ONE profile
    before any hermes-agent import happens, then calls
    `gateway.run.start_gateway()` directly -- the exact same top-level
    "run until interrupted" entry point `hermes gateway run`'s own CLI
    command calls, confirmed to already construct+connect every
    configured platform adapter (including `api_server`) on its own, no
    manual adapter wiring needed. `engine.py`'s own
    `_build_runner_and_adapter()` (used only by the embedded transport)
    is untouched.
  - New `backend/bot_processes.py` -- owns the worker pool from the
    FastAPI process: deterministic port allocation persisted to its own
    registry file (survives a backend restart without reassigning a bot
    to a port a still-running worker is on), `ensure_bot_process_running()`
    (spawns via `multiprocessing` with the `"spawn"` start method if not
    already tracked alive, waits for a real `GET /health`),
    `stop_bot_process()` (SIGTERM via `Process.terminate()`, SIGKILL
    escalation if uncooperative), `reap_idle()`. Liveness is a real check
    (`os.kill(pid, 0)`), not a cache read -- a pid recorded by a prior
    backend process that's since died is treated as orphaned, matching
    this session's own `session_turn_leases` methodology applied to
    process ownership instead. Covered by `tests/test_bot_processes.py`
    (21 tests), every subprocess/httpx call mocked -- no real process
    spawning in the suite (a real bug hit live while building this: the
    existing `test_backend.py` create_bot/update_bot/delete_session tests
    started spawning genuine `multiprocessing.Process` children and
    blocking past the test harness's own timeout the moment
    `ensure_bot_process_running()` went unmocked -- fixed by isolating
    `BOT_PROCESSES_STATE_PATH` to a tempdir and auto-mocking
    `ensure_bot_process_running` in the shared `client` fixture).
  - `engine.py`'s and `main.py`'s own (intentionally duplicated, to avoid
    a circular import) `_bot_base(profile)` now resolve every profile --
    `default` no longer special-cased -- through
    `bot_processes.get_port(profile)` instead of a shared-gateway URL or
    `/p/<profile>/` prefix. `_profile_scope`'s http-transport no-op branch
    needed no code change, just an updated comment: scoping now happens
    by construction (a dedicated worker is already unscoped to its one
    profile for its whole lifetime), not on the wire.
  - Lifecycle is dynamic per bot, not a manual flag: a bot with at least
    one ENABLED routine is classified keep-warm (its own cron scheduler
    only runs while its own worker is alive -- there is no external
    "wake me before my cron fires" mechanism, so a routine-bearing bot
    has to stay up for that routine to ever fire at all); `default` is
    always keep-warm (it also carries messaging connectors, replacing the
    old always-on `GATEWAY_PID`'s role); everything else is on-demand --
    woken by `send_to_bot`/`stream_to_bot`/`create_bot`/`update_bot`/
    `delete_session` calling `ensure_bot_process_running()` before
    dialing (all of them always do real HTTP against a bot's own worker,
    independent of `ZBOTS_CHAT_TRANSPORT` -- confirmed live for the
    session-lock/delete-session pair, which predate that flag entirely),
    reaped by a periodic sweep (`BOT_IDLE_REAP_SECONDS`, default 30m)
    that never touches a keep-warm bot. New `_keep_warm_bots()`/
    `_spawn_keep_warm_bots_and_start_reaper()` (FastAPI startup)/
    `_idle_reap_loop()`/`_stop_all_bot_processes()` (FastAPI shutdown) in
    `main.py`, covered by new tests in `test_backend.py`.
  - `entrypoint.sh`'s old unconditional `GATEWAY_PID` block (`hermes
    gateway run`, the shared multiplexed process) is now conditional on
    `ZBOTS_CHAT_TRANSPORT`: unchanged under the `embedded` default (still
    the only thing touching `HERMES_HOME` for real gateway work, exactly
    as before this change), skipped entirely under `http` -- where
    `default`'s own dedicated worker takes over that role instead.
    Deliberately NOT run alongside each other: two separate processes
    constructing a `GatewayRunner` against the same `HERMES_HOME`/session
    store concurrently was never tested and not worth the risk.
  - `gateway.multiplex_profiles: true` (added earlier this session) is
    left in place in the bootstrap config -- now dead/unused config since
    nothing generates a `/p/<profile>/`-prefixed URL any more, but
    harmless, and an already-bootstrapped volume's on-disk config.yaml
    wouldn't pick up a bootstrap-heredoc removal anyway. Not worth the
    risk of a live edit for a purely cosmetic cleanup.
  - Real memory/cold-start cost per worker still needs live measurement
    on zbots-dev before `ZBOTS_CHAT_TRANSPORT=http` is turned on there --
    zbots-dev was already at ~55% of its 1024MB budget idle under the old
    architecture. `mcp__zorc__request_memory_increase`/`approve_action`
    is the known-working path once real numbers are in hand.
  - Real bug found live, deploying this: `ZBOTS_CHAT_TRANSPORT` had
    carried over as `http` from earlier Phase 1 testing, so this landed
    live immediately -- and the startup handler's single-attempt
    `get_roster()` call raced the dashboard process (a separate
    background subshell in `entrypoint.sh`, no explicit ordering/wait
    against the FastAPI backend's own startup) not being ready yet,
    failed, and silently gave up: `default`'s worker never spawned, chat
    broken container-wide until a manual redeploy. Fixed two ways: new
    `_get_roster_with_retry()` (5 attempts, 2s apart) absorbs the
    ordinary startup race directly; the periodic sweep (renamed
    `_idle_reap_loop` -> `_bot_lifecycle_sweep_loop`, extracted
    `_ensure_keep_warm_bots_running()` shared by both) now re-ensures
    every keep-warm bot on each cycle instead of only ever trying once at
    boot, so a keep-warm bot that failed to start (or was manually
    stopped) self-heals within one `IDLE_REAP_INTERVAL_SECONDS` instead
    of staying down forever. Restored service by reverting zbots-dev to
    `embedded` immediately on discovery; redeployed with the fix before
    considering `http` again. Covered by new tests in `test_backend.py`.

### Fixed
- `gateway.multiplex_profiles` was never set anywhere in zBots' bootstrap
  config, so it defaulted to `False` -- meaning the real api_server
  platform's `/p/<profile>/` URL-prefix routing silently no-op'd,
  treating every request as the "default" profile no matter which
  profile the URL actually named. Confirmed live: `_lock_active_session_model`'s
  `POST /api/sessions/{id}/model` call for a non-default bot kept
  reporting success but never actually took effect, until switching to
  `/models/activate` (global main slot, unaffected by profile-scoping)
  worked around it -- this is almost certainly why that bug existed at
  all. Fixed by adding `gateway: {multiplex_profiles: true}` to
  `entrypoint.sh`'s bootstrapped config.yaml, applied live to the running
  volume; verified `/p/coder/api/sessions` and `/p/butler/api/sessions`
  now each return only that bot's own session, not the merged pool of
  all 50+ sessions across every bot.
  - Turning multiplexing on surfaced a second real gap: hermes-agent
    deliberately treats `API_SERVER_KEY` as a per-profile secret once
    multiplexing is active (not a global env var -- see vendor's
    `agent/secret_scope.py`), so every existing bot's own `<profile>/.env`
    needed the same key backfilled by hand, and any *new* bot would hit
    the identical bug the moment it tried to lock its own session. Fixed
    with `_provision_profile_api_server_key()` in `main.py`, called from
    `create_bot()` right before the existing session-lock call that
    depends on it -- verified live against a fresh bot, `.env` populated
    automatically, real chat round-trip works.

### Changed
- Phase 1 of moving zBots' chat path onto hermes-agent's native
  architecture: `engine.py`'s session/chat calls (list/create sessions,
  chat, message read, and per-token streaming) can now go over real
  loopback HTTP to the api_server gateway process instead of the
  `aiohttp.test_utils.make_mocked_request` in-process embedding that was
  the only option before -- the design `main.py`'s own module docstring
  already described, and the same surface `_lock_active_session_model`/
  `delete_session` used successfully already. Selected by
  `ZBOTS_CHAT_TRANSPORT` (`embedded` default, `http` opt-in) read once at
  process start; every session-bookkeeping helper and the resilience
  retry/rollover logic keep their exact current behavior either way, only
  the transport underneath changed. `_profile_scope` becomes a no-op
  under `http` -- scoping happens on the wire via `/p/<profile>/`
  (requires the `multiplex_profiles` fix above) instead of a contextvar.
  - New `_call_handler_http`/`_run_stream_attempt_http`, covered by
    `tests/test_engine_http_transport.py` (profile-prefix routing,
    header/body forwarding, transport-failure-becomes-500, and SSE-frame
    reassembly across a real chunked response's chunk boundaries -- the
    one thing the old in-memory queue couldn't exercise since every
    `write()` there was already one whole frame).
  - Live-tested on zbots-dev: `http` transport worked immediately for the
    `default` bot but every other bot failed with `"Unknown provider
    '<name>'"`. Root cause: under `multiplex_profiles`, each profile is a
    fully independent Hermes install (`gateway/run.py`'s
    `_profile_runtime_scope` redirects `HERMES_HOME` to that profile's own
    directory, no inheritance from `default`) -- but `create_bot()`/
    `update_bot()` have only ever written a `model: {provider, default}`
    *reference* into a profile's own `config.yaml`, never the `providers:`
    block that actually defines what that provider IS (base_url, key_env).
    This worked by accident under the embedded transport the whole time
    zBots has existed: `engine._get_adapter()` builds ONE shared adapter
    from `default`'s config and reuses it regardless of which profile a
    call is "scoped" to, so provider resolution never actually depended on
    the scoped profile's own config having anything. Fixed with
    `_sync_profile_provider()`, called from both `create_bot()` and
    `update_bot()` right after a provider is assigned -- copies that
    provider's real definition from the default profile's own config into
    the target profile's, via the same `PUT /api/config` (profile-scoped)
    real hermes-agent already exposes. Skips built-in routing providers
    (openrouter/auto/custom/anything in `PROVIDER_REGISTRY`) since those
    resolve through native code paths, not a `providers:` entry. Covered
    by new tests in `test_backend.py`.
  - Re-testing after that fix (and after backfilling the `providers:`
    block into every existing secondary profile -- `butler`, `coder`,
    `coder-fast`, `hydration-reminder`) surfaced the same gap one layer
    down: a synced provider with real credentials (`deepseek-flash`)
    still 401'd for any non-default bot with `"Authentication Fails ...
    invalid"`, while zBots' own unauthenticated sglang endpoints (`coder`
    on `sglang-thor`) worked immediately. `key_env` (e.g.
    `HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY`) only resolves through the
    multiplexed profile's own `.env`-backed secret scope, same mechanism
    `API_SERVER_KEY` needed -- confirmed live the container's own process
    environment has no such variable at all, only the root/default
    profile's own `.env` does. Fixed with
    `_provision_profile_provider_secret()`, called from
    `_sync_profile_provider()` right after the `providers:` block sync --
    copies the real key value from the root profile's `.env` into the
    target profile's own, idempotently, same convention as
    `_provision_profile_api_server_key()`. No-ops cleanly for an
    unauthenticated provider (no `key_env` at all). Covered by new tests
    in `test_backend.py`.
  - Full live verification pass on zbots-dev with both fixes applied and
    every existing secondary profile backfilled (`providers:` block +
    credential): blocking chat on `default` and three non-default bots
    (`hydration-reminder`/`deepseek-flash`, `butler`/`deepseek-flash`,
    `coder`/`sglang-thor` unauthenticated); streaming chat on a
    non-default bot, including one real rollover observed live (attempt 1
    silently produced no content, attempt 2 retried and streamed the real
    answer -- confirmed nothing from attempt 1 reached the client,
    matching `_chunks()`'s own suppression contract) and one clean
    single-attempt completion; `GET /bots/{name}/messages` on a
    non-default bot returned correctly with no special-casing needed,
    resolving the plan's open question 2 (the embedded-path-only
    secondary-profile-scoping workaround does not appear to be needed
    under real `/p/<profile>/` routing, though `get_bot_messages`' own
    workaround comment is left in place since it's specific to the
    embedded transport and does nothing under `http`); session-lock via
    `PATCH /bots/{name}` (`_lock_active_session_model`) on a non-default
    bot; roster still renders every bot correctly afterward. zBots-dev is
    intentionally left running `ZBOTS_CHAT_TRANSPORT=http` for a longer
    soak/observation period before any further rollout decision -- `main`
    is untouched and stays on the embedded transport throughout.

### Added
- Coding tasks now get delegated to a dedicated "coder" bot instead of
  handled inline: the shared RESPONSE_STYLE guardrail (`persona.py`) tells
  every bot to hand off real coding/development work via `message_bot`
  rather than attempting it in its own context. "coder" runs OpenCode
  and Qwen Code -- real external CLI coding agents, already supported by
  hermes-agent's own `opencode` skill -- against a dedicated Qwen3.8-27B
  instance on the Thor GPU box (256K context, the model's real max --
  no variant of this checkpoint supports the 1M figure that was asked
  for). Both CLIs and Node 22 (Debian's own nodejs/npm package is one
  major behind Qwen Code's declared minimum) are now baked into the
  Dockerfile so a fresh deploy has them from first boot, not just after
  a live install a redeploy would lose.

### Fixed
- Routines silently went nowhere. `create_routine` hardcoded
  `deliver: "local"`, which runs a routine's prompt in its own private
  `cron_<id>_<timestamp>` session and posts the result nowhere -- confirmed
  live, a completed test routine left zero trace across 200 fetched
  messages. Every routine ever created through zBots' own Add Routine form
  was invisible the moment it fired. Tried Hermes' native "bot-chat"
  deliver target next; it works for recurring jobs but silently deletes a
  finite one-shot job outright on fire (confirmed live, twice, with no log
  trace either time -- leading theory is its delivery subprocess's own
  gateway-boot-style reconcile treating the just-fired one-shot as an
  orphan). Landed on the same mechanism this gateway's own pre-existing
  agentic cron jobs already use successfully: the prompt itself now
  instructs the model to call its `message_bot` tool
  (`backend/supervisor_mcp.py`, already registered for every profile) to
  deliver the result, with `deliver` staying `"local"` (the delivery
  already happened as a tool call, so auto-delivering on top would double
  it). Confirmed live end-to-end, including cross-bot: a routine on
  `default` targeting `butler` landed a real turn in butler's own visible
  chat, butler replied to it normally.
- Every routine zBots created was born unpinned (no explicit
  provider/model), which trips Hermes' own provider/model drift guard
  (`cron/scheduler.py`, #44585) the moment the bot's model is later
  switched -- it fails the job closed (no run, no charge) rather than
  silently switching spend. Confirmed live and exactly matching a real
  report ("your hourly hydration reminder has been failing since the
  model switch"): the actual `hydration-reminder` job had a 13-run
  failure streak, all `[drift_skip]`, and a second pre-existing job
  (`bobby-checkin`) was in the same state. `create_routine` now resolves
  and pins the bot's actual current (provider, model) at creation time,
  which the drift guard treats as intentional and never touches; both
  broken production jobs were re-pinned to their current live model and
  confirmed firing clean (`last_status: "ok"`, failure streak reset to 0,
  a real reply landed in that bot's chat) as part of this fix, not just
  the code path for future routines.
- A cross-bot delivery target became reachable in the Add Routine form
  itself: a "Deliver to: <bot>" dropdown (any other live bot, populated
  from the roster) alongside the existing "this bot's chat" default,
  wired through to the same `message_bot`-based delivery above --
  previously only achievable by a bot agentically creating its own cron
  job with the right prompt, never from the UI.
- Two bots' recurring "every Nh" check-ins (`bobby-checkin`,
  `butler-checkin`) were permanently stuck in `state: "error"` with
  `next_run_at: null` after their very first tick, surfacing a misleading
  "is the 'croniter' package installed?" error that had nothing to do
  with croniter. Root cause, in the vendored `cron/jobs.py`'s
  `compute_next_run`: an interval schedule's next-run computation only
  ever read a `"minutes"` key, but these two jobs were persisted as
  `{"kind": "interval", "hours": 10}` (built directly, not through
  `parse_schedule`'s string-to-minutes normalization) -- `minutes` was
  always `None`, so the function returned `None` unconditionally and the
  caller reported it as a generic recurring-schedule failure. Fixed
  `compute_next_run` (plus the two other next-run/interval-length
  helpers in `cron/scheduler.py` and `tools/blueprints.py` that had the
  identical minutes-only assumption) to also accept `hours`/`days`.
  Confirmed live: `bobby-checkin` immediately got a real computed
  `next_run_at` again instead of `null`.
- The card flicker came back right after the previous revert ("cards are
  still flickering") because the revert only undid one attempted fix, not
  the actual root cause: `loadMessages` unconditionally wiped and rebuilt
  the entire pane on every 5-second poll regardless of whether the server
  response had changed at all. Messages never visibly flickered from this
  because their rebuild is synchronous (one paint, no gap); preview cards
  did, because they're reinserted through an async step, so even a totally
  no-op poll opened a real window with zero cards on screen. Fixed at the
  actual source instead of tuning the async step again: `loadMessages` now
  compares the raw fetched rows against what's already rendered and skips
  calling `renderMessages` entirely when nothing changed. Confirmed live
  with a `MutationObserver` on the messages pane plus 50ms-interval card
  sampling across three-plus full poll cycles (18s): zero DOM mutations
  observed, card count locked at a constant value the entire time.
- The synchronous cache-hit fast path for preview cards (`buildPreviewCardSync`)
  made the flicker worse instead of fixing it -- reported live right after
  shipping ("flickering is even worse"). Reverted outright rather than
  attempting a second patch on top of an approach that had just been
  contradicted by direct user observation; the permanent-cache/async
  `buildPreviewCard` path from the previous fix is back in place and
  confirmed stable again (message and card counts held constant across a
  full 20-second window post-revert).
- The Google Calendar/Gmail connect flow was dumping five separate
  clickable links into one message (project selector, API library, OAuth
  client creation, test-user audience page, credentials download) when
  reported live only the last was actually needed to act on -- Google's
  own console pages already let you select/create a project and enable
  an API inline from the credentials screen itself, so the earlier links
  were redundant clicks, not separate required destinations. Added a
  RESPONSE_STYLE guardrail (`persona.py`) so any bot walking a user
  through an external OAuth/API setup gives exactly one primary link per
  message and explains sub-steps as plain text next to it, saving a
  second link only for a genuinely different later destination (the
  real approval/consent URL once setup is done). Patched live onto the
  `default` bot's soul and confirmed via `GET /bots/default/soul`.
- `/bots/` (the whole static frontend -- app.js, styles.css, etc.) had no
  Cache-Control header at all, leaving every browser to its own
  heuristic caching. Real confusion this caused live: a fix would ship
  and be directly verified working against the server, then get reported
  right back as still broken -- the browser was still serving whatever
  copy it had fetched before, with no indication anything was stale.
  Added `Cache-Control: no-cache` (not `no-store` -- the browser still
  keeps its copy, but must revalidate with the server first every time,
  via the ETag/Last-Modified nginx already sends). An unchanged file is
  still a fast 304; a changed one is now picked up immediately on the
  next load instead of requiring a manual hard-refresh.
- Still-present periodic flicker reported live right after the previous
  round of flicker fixes shipped ("chat is still refreshing all the
  time") -- the preview cache's TTL (15s, raised from an even-worse 4s
  earlier the same day) still landed a real network re-fetch right at
  its own expiry boundary on whichever poll happened to land after it,
  visible as cards vanishing and reappearing roughly every 15 seconds.
  No finite TTL was ever going to fully fix this -- it can only trade
  flicker frequency for flicker frequency. Reverted to a permanent
  session cache; the real fix for staleness is the RESPONSE_STYLE
  guardrail already shipped (bots save a revision under a new filename
  instead of overwriting), which sidesteps the cache-invalidation
  question entirely by giving a genuinely different version a genuinely
  different, never-cached path. Confirmed live: message count in the
  pane held at a constant 44 across a full 35-second observation window,
  not a single dip.
- Every routine card in the Routines panel rendered its schedule as the
  literal text "[object Object]" -- a job's `schedule` field is a
  structured object (`{kind, run_at, display}`), and the card read it
  directly instead of the ready-made `schedule_display` string Hermes
  already provides.

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
