# Changelog

All notable changes to zBots are recorded here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions are
tagged on `main`.

## [Unreleased]

### Added
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
