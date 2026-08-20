# zBots

An open-source multi-agent bot platform built on top of
[Hermes Agent](https://hermes-agent.nousresearch.com) -- the parts of a
real multi-bot experience Hermes' own web dashboard doesn't have yet: a bot
roster with per-bot chat and group chats, routines, and a full admin
surface (models & providers, MCP servers, skills, environment, cron,
plugins, webhooks, files, logs, system) in one clean, mobile-responsive UI.

Bring your own model provider -- any OpenAI-compatible endpoint (Ollama,
vLLM, llama.cpp, a hosted API, whatever) works, configured entirely through
the UI, no config file editing required.

See [CREDITS.md](./CREDITS.md) for what Hermes Agent provides vs. what
zBots adds.

## Architecture

- **`backend/`** -- a small FastAPI app that talks to two real Hermes Agent
  surfaces: the dashboard's session-authenticated REST API (profile/model/
  MCP/skills/env/cron/etc. CRUD) and the `api_server` platform's
  Bearer-authenticated API (actual chat). No database of its own beyond a
  small local JSON file for state Hermes has no concept of (hidden bots,
  avatar choices, group definitions, per-bot title, and session-family
  tracking for the resilience layer below).
- **`frontend/`** -- vanilla HTML/CSS/JS, no build step, no framework, no
  external CDN dependency. A shared nav shell (`shell.js`/`icons.js`)
  renders on every page; each page is its own small HTML/JS pair.

### The resilience layer

Hermes Agent has a real, currently-unresolved upstream bug where a chat
session can fail every turn after its first
([NousResearch/hermes-agent#89119](https://github.com/NousResearch/hermes-agent/issues/89119),
[#16123](https://github.com/NousResearch/hermes-agent/issues/16123)) --
hits hardest on self-hosted/custom providers, which is the whole point of
this project. `backend/main.py`'s `send_to_bot()` works around it: each
bot's session id is tracked in local state, and a failed turn rolls over to
a fresh session (kept, not deleted) rather than surfacing the error.
`get_bot_messages()` merges a bot's whole session family back into one
continuous transcript, so the rollover is invisible from the UI. Read the
docstring for the full investigation -- six workarounds were tried and
ruled out live before landing on this one.

## Running it

zBots expects to run alongside a Hermes Agent instance, reachable on
the loopback interface (it's designed to run as a sidecar process in the
same container/host as Hermes, not as a public-facing service on its own --
see [Deployment](#deployment)).

Environment variables the backend reads:

| Variable | Purpose |
|---|---|
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `_PASSWORD` | Logs into Hermes' dashboard session API (`/auth/password-login`) on first request |
| `API_SERVER_KEY` | Bearer token for Hermes' `api_server` platform (actual chat) |
| `BOTS_UI_STATE_PATH` | Where to persist local state (default `/opt/data/bots-ui-state.json`) |
| `BOTS_UI_AVATAR_DIR` | Where uploaded avatar images live (default `/opt/data/bots-ui-avatars`) |

```bash
cd backend
pip install fastapi uvicorn httpx
uvicorn main:app --host 127.0.0.1 --port 8643
```

Serve `frontend/` as static files behind the same reverse proxy, with
`/bots-api/*` proxied to the backend (prefix stripped) and everything else
served as static files with `index.html` as the SPA-ish fallback. See
[zaindroid/hermes-agent-wrapper](https://github.com/zaindroid/hermes-agent-wrapper)
for a real, working reference deployment (nginx config, Dockerfile,
entrypoint).

## Deployment

zBots is consumed as a git submodule by
[hermes-agent-wrapper](https://github.com/zaindroid/hermes-agent-wrapper),
which bundles it into the same container as Hermes Agent itself (nginx in
front, reverse-proxying `/bots/*` to static files and `/bots-api/*` to this
backend). That's the actual reference deployment this project is developed
and tested against.

## Roadmap

- Local desktop app (packaged executable) and a CLI, talking to the same
  backend API, for running against a local Hermes instance without a
  browser
- Hosted/multi-tenant version with per-user accounts and usage-based
  pricing, still fully BYOK for model providers
- Native chat page (the current "Chat (classic)" link opens Hermes' own
  dashboard chat -- a from-scratch rebuild is a materially different scope
  than the admin pages here and hasn't been attempted yet)

## License

MIT -- see [LICENSE](./LICENSE).
