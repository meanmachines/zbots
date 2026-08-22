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
  (profile/model/MCP/skills/env/cron/etc. CRUD) and a Bearer-authenticated
  API server (actual chat). No database of its own beyond a small local
  JSON file for state the engine has no concept of (hidden bots, avatar
  choices, group definitions, per-bot title, and session-family tracking
  for the resilience layer below).
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

zBots expects to run alongside its agent engine, reachable on the loopback
interface (it's designed to run as a sidecar process in the same
container/host as the engine, not as a public-facing service on its own --
see [Deployment](#deployment)).

Environment variables the backend reads:

| Variable | Purpose |
|---|---|
| `HERMES_DASHBOARD_URL` | Engine dashboard base URL (default `http://127.0.0.1:9119`) |
| `HERMES_API_SERVER_URL` | Engine api_server base URL (default `http://127.0.0.1:8642`) |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` / `_PASSWORD` | Logs into the engine's dashboard session API (`/auth/password-login`) on first request; also used by the standalone container as the nginx basic-auth gate for `/bots/*` |
| `API_SERVER_KEY` | Bearer token for the engine's api_server platform (actual chat) |
| `BOTS_UI_STATE_PATH` | Where to persist local state (default `/opt/data/bots-ui-state.json`) |
| `BOTS_UI_AVATAR_DIR` | Where uploaded avatar images live (default `/opt/data/bots-ui-avatars`) |
| `BOTS_UI_API_KEY` | Optional shared secret: when set, every backend route except `/health` requires `Authorization: Bearer <key>` (defense-in-depth for non-browser clients; browser deployments should keep the nginx auth layer) |

```bash
cd backend
pip install fastapi uvicorn httpx python-multipart
uvicorn main:app --host 127.0.0.1 --port 8643
```

Serve `frontend/` as static files behind the same reverse proxy, with
`/bots-api/*` proxied to the backend (prefix stripped) and everything else
served as static files with `index.html` as the SPA-ish fallback.

## Deployment

**Reference deployment:** zBots is consumed as a git submodule by a
companion wrapper repo, which bundles it into the same container as the
agent engine itself (nginx in front, reverse-proxying `/bots/*` to static
files and `/bots-api/*` to this backend). That's the actual deployment this
project is developed and tested against.

**Standalone container:** the repo also ships its own `Dockerfile` /
`nginx.conf` / `entrypoint.sh` for running zBots as its own container
pointed at an engine instance elsewhere (or as a sidecar on the same host):

```bash
docker build -t zbots .
docker run -p 8080:8080 \
  -e HERMES_DASHBOARD_URL=http://engine-host:9119 \
  -e HERMES_API_SERVER_URL=http://engine-host:8642 \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=secret \
  -e API_SERVER_KEY=changeme \
  -v zbots-data:/opt/data \
  zbots
```

The container exposes the UI on port 8080 under `/bots/` and the API under
`/bots-api/`, gated by nginx basic auth using the same
`HERMES_DASHBOARD_BASIC_AUTH_*` credentials. `app.yaml` declares the app for
platforms that consume it (zorc-style deployment).

## Development / tests

```bash
pip install -r requirements-dev.txt
pytest tests
```

The test suite mocks both engine surfaces, so it runs without a live
engine instance.

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
