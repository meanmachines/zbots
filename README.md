# zBots

A super-powered multi-agent bot platform by **MeanMachines Technologies** —
a bot roster with per-bot chat and group chats, routines, and a full admin
surface (models & providers, MCP servers, skills, environment, cron,
plugins, webhooks, files, logs, system) in one clean, mobile-responsive UI.

Bring your own model provider -- any OpenAI-compatible endpoint (Ollama,
vLLM, llama.cpp, a hosted API, whatever) works, configured entirely through
the UI, no config file editing required.

See [docs/CREDITS.md](./docs/CREDITS.md) for open-source acknowledgments.

## Architecture

- **`backend/`** -- a small FastAPI app that talks to the underlying agent
  engine's two real surfaces: a session-authenticated REST API
  (profile/model/MCP/skills/env/cron/etc. CRUD, against the in-container
  dashboard backend over loopback HTTP -- see `entrypoint.sh`) and chat,
  which runs in-process against the vendored engine (`backend/engine.py`,
  no network hop). No database of its own beyond a small local JSON file
  for state the engine has no concept of (hidden bots, avatar choices,
  group definitions, per-bot title, and session-family tracking for the
  resilience layer below).
- **`vendor/hermes-agent/`** -- a frozen, pinned snapshot of the
  underlying agent engine (no upstream git remote -- see
  `vendor/VENDORED_COMMIT.md` for the pinned commit and update policy).
- **`frontend/`** -- vanilla HTML/CSS/JS, no build step, no framework, no
  external CDN dependency. A shared nav shell (`shell.js`/`icons.js`)
  renders on every page; each page is its own small HTML/JS pair.

### The resilience layer

The underlying engine has a real chat-session bug where a session can fail
every turn after its first -- hits hardest on self-hosted/custom providers,
which is the whole point of this project. `backend/main.py`'s
`send_to_bot()` works around it: each bot's session id is tracked in local
state, and a failed turn rolls over to a fresh session (kept, not deleted)
rather than surfacing the error. `get_bot_messages()` merges a bot's whole
session family back into one continuous transcript, so the rollover is
invisible from the UI. See [docs/CREDITS.md](./docs/CREDITS.md) and the
`send_to_bot()` docstring for the full investigation and upstream tracking
links -- six workarounds were tried and ruled out live before landing on
this one.

## Running it

zBots is a standalone product: one container, nginx in front, the engine
vendored inside it. Chat/sessions run in-process against the vendored
engine (see `backend/engine.py`); profile/config management (roster, MCP
servers, skills, env vars, cron, webhooks, files) runs against a second
in-container process (the engine's own headless backend, `hermes serve` --
see `entrypoint.sh`). Nothing here talks to an engine instance running
anywhere else.

Environment variables `entrypoint.sh` reads on first boot:

| Variable | Purpose |
|---|---|
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `_PASSWORD` | Required. Credentials for the in-container dashboard backend's auth gate, and reused as the nginx basic-auth gate for `/bots/*` and `/bots-api/*` |
| `ZBOTS_MODEL_BASE_URL` | Required on first boot. Base URL of an OpenAI-compatible chat completions endpoint -- see [Model providers](#model-providers) below |
| `ZBOTS_MODEL_NAME` | Required on first boot. The model id to request at that endpoint |
| `ZBOTS_MODEL_API_KEY` | Optional. Sent as the provider's API key; omit for a local/no-auth endpoint |
| `API_SERVER_KEY` | Bearer token the in-process engine calls use internally; generate one, don't reuse it elsewhere |
| `BOTS_UI_STATE_PATH` | Where to persist local state (default `/opt/data/bots-ui-state.json`) |
| `BOTS_UI_AVATAR_DIR` | Where uploaded avatar images live (default `/opt/data/bots-ui-avatars`) |
| `BOTS_UI_API_KEY` | Optional shared secret: when set, every backend route except `/health` requires `Authorization: Bearer <key>` (defense-in-depth for non-browser clients; browser deployments should keep the nginx auth layer) |

The model provider config above only gets written once, into the
persistent profile at `/opt/data/engine-profile/config.yaml` -- editing
those env vars after first boot has no effect; change the model from the
UI (or edit that file directly) instead.

### Model providers

`ZBOTS_MODEL_BASE_URL`/`ZBOTS_MODEL_NAME`/`ZBOTS_MODEL_API_KEY` work with
any OpenAI-compatible chat completions endpoint -- a self-hosted vLLM/
llama.cpp/Ollama server, or a real hosted provider. DeepSeek's own API is
OpenAI-compatible, so pointing at it needs no code changes, just:

```
ZBOTS_MODEL_BASE_URL=https://api.deepseek.com/v1
ZBOTS_MODEL_NAME=deepseek-chat
ZBOTS_MODEL_API_KEY=<your DeepSeek API key>
```

(Not yet verified end-to-end against a real DeepSeek key by this project
-- the generic custom-provider path is exercised daily against other
OpenAI-compatible endpoints, so this should work as-is, but hasn't
specifically been confirmed.)

For local backend-only iteration without a full container build:

```bash
cd backend
pip install fastapi uvicorn httpx python-multipart
uvicorn main:app --host 127.0.0.1 --port 8643
```

This gets you the roster/API surface for quick edits, but chat needs the
vendored engine and a real profile -- see `entrypoint.sh` for what that
setup actually requires; running the full container is the realistic way
to exercise chat locally.

## Deployment

The repo ships its own `Dockerfile` / `nginx.conf` / `entrypoint.sh` --
build and run it as-is:

```bash
docker build -t zbots .
docker run -p 8080:8080 \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=secret \
  -e ZBOTS_MODEL_BASE_URL=https://api.deepseek.com/v1 \
  -e ZBOTS_MODEL_NAME=deepseek-chat \
  -e ZBOTS_MODEL_API_KEY=changeme \
  -v zbots-data:/opt/data \
  zbots
```

The container exposes the UI on port 8080: `/` redirects to `/bots/`, the
API lives under `/bots-api/`, both gated by nginx basic auth using the
`HERMES_DASHBOARD_BASIC_AUTH_*` credentials above. `/health`, `/ready`,
`/version` and `/openapi.json` stay unauthenticated for orchestrator
checks. `app.yaml` declares the app for platforms that consume it
(zorc-style deployment).

## Development / tests

```bash
pip install -r requirements-dev.txt
pytest tests
```

The test suite mocks both engine surfaces, so it runs without a live
engine instance.

### Branches

`main` is stable and deployable -- nothing gets pushed to it directly.
Ongoing work happens on `dev` (or a feature branch merged into `dev`),
and reaches `main` only through a pull request once CI is green. CI
(`.github/workflows/ci.yml`) runs the test suite on every push to either
branch and on every pull request.

## Roadmap

- Local desktop app (packaged executable) and a CLI, talking to the same
  backend API, for running against a local engine instance without a
  browser
- Hosted/multi-tenant version with per-user accounts and usage-based
  pricing, still fully BYOK for model providers
- Native chat page (per-bot chat exists today inside the Bots roster; a
  standalone general chat page with streaming and tool-call rendering is a
  materially different scope than the admin pages here and hasn't been
  attempted yet)
- Distributed agent mesh: zBots instances on phone, tablet, PC, and server
  sharing context and collaborating via MCP, not required to share a model
  or runtime

## License

MIT -- see [LICENSE](./LICENSE). Third-party license text (the vendored
engine snapshot in `vendor/`) is in
[THIRD_PARTY_LICENSES.md](./THIRD_PARTY_LICENSES.md). Acknowledgments in
[docs/CREDITS.md](./docs/CREDITS.md).
