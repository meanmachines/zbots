"""Bots UI backend -- a small companion API that gives the web dashboard the
multi-bot management surface Hermes' desktop app has natively (Bot Mode) and
the web dashboard doesn't ship.

Talks to two ALREADY-REAL Hermes surfaces, both loopback-only from inside
this same container:

  - the dashboard's own REST API on 127.0.0.1:9119 (HTTP basic auth) for
    profile CRUD, config, and cron/routines -- exactly what the bundled
    ProfilesPage/CronPage already call.
  - the api_server platform for actually chatting with a bot: this is the
    same session-based chat protocol `hermes peer dm` uses (find-or-create
    a session titled "Bot Chat" for that profile, POST a message, read the
    reply). Under ZBOTS_CHAT_TRANSPORT=http, each bot runs its OWN
    dedicated worker process (bot_processes.py) with its own port -- not
    one shared gateway on a fixed port -- see bot_processes.py's own
    module docstring for why (removing a real, twice-found class of
    credential/config-scoping bug that a single shared multiplexed
    process is an ongoing surface for).

State this app owns that Hermes has no native concept of (hidden bots,
avatar choices, group definitions) lives in a small JSON file on the
persistent volume, not a database -- there's no scale here that needs one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# Loopback defaults match the reference deployment (hermes-agent-wrapper runs
# Hermes' dashboard and api_server on these ports inside the same container).
# Standalone deployments point these at a reachable Hermes instance instead.
DASHBOARD_BASE = os.environ.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119").rstrip("/")
API_SERVER_BASE = os.environ.get("HERMES_API_SERVER_URL", "http://127.0.0.1:8642").rstrip("/")

DASHBOARD_USER = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "")
DASHBOARD_PASS = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "")
API_SERVER_KEY = os.environ.get("API_SERVER_KEY", "")

# Optional shared secret for direct backend access. When set, every route
# except /health requires "Authorization: Bearer <key>". Browser deployments
# should still keep a reverse-proxy auth layer (nginx basic auth in the
# reference deployment) -- this is defense-in-depth for when the API is
# reached by non-browser clients or accidentally exposed without the proxy.
BOTS_UI_API_KEY = os.environ.get("BOTS_UI_API_KEY", "")

STATE_PATH = Path(os.environ.get("BOTS_UI_STATE_PATH", "/opt/data/bots-ui-state.json"))
AVATAR_DIR = Path(os.environ.get("BOTS_UI_AVATAR_DIR", "/opt/data/bots-ui-avatars"))
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

# Group-chat safety caps (docs describe "10 messages per send, 3 rounds" for
# the desktop implementation; this companion app approximates that -- see
# _run_group_turn for exactly what "approximates" means here).
GROUP_MAX_ROUNDS = 3
GROUP_MAX_MESSAGES = 10

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_MENTION_RE = re.compile(r"@([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})")

app = FastAPI(title="Hermes Bots UI backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _optional_api_key_auth(request, call_next):
    if BOTS_UI_API_KEY and request.url.path != "/health":
        provided = request.headers.get("Authorization", "")
        if provided != f"Bearer {BOTS_UI_API_KEY}":
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
    return await call_next(request)


_state_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# Local state (hidden bots / avatar choices / group definitions)
# --------------------------------------------------------------------------

STATE_VERSION = 1


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "hidden": [],
        "avatars": {},
        "groups": {},
        "titles": {},
        "active_sessions": {},
        # What we actually session-locked for a bot (see
        # _lock_active_session_model's docstring for why Hermes' own
        # profile-level provider field can't be trusted for custom
        # providers) -- the roster prefers this over the profile's own
        # value when present, since it's the one guaranteed correct.
        "locked_models": {},
        # A bot's own lifecycle category -- zBots' own concept, not a
        # hermes-agent-native one, so it lives here rather than in the
        # profile's own config.yaml. See _infer_bot_category()'s own
        # docstring and CATEGORIES below for what each value means; a bot
        # with no entry here is "general" (the implicit default, covers
        # every bot that existed before this feature).
        "categories": {},
    }


def _migrate_state(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an older state file forward.

    Currently version 1 is the only schema and older files (pre-versioning)
    simply lack the key; the migration is a no-op that stamps the current
    version. Keeping the function separate means future fields have a single,
    explicit place to be added instead of being sprinkled into _read_state.
    """
    if data.get("version") != STATE_VERSION:
        data["version"] = STATE_VERSION
    return data


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    data = _migrate_state(data)
    data.setdefault("hidden", [])
    data.setdefault("avatars", {})
    data.setdefault("groups", {})
    data.setdefault("titles", {})
    data.setdefault("active_sessions", {})
    data.setdefault("locked_models", {})
    data.setdefault("categories", {})
    return data


def _pretty_title(name: str) -> str:
    """Fallback display title when the user hasn't set one: 'my-bot' -> 'My Bot'."""
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", name) if word)


def _write_state(data: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE_PATH)


async def _mutate_state(fn):
    async with _state_lock:
        data = _read_state()
        result = fn(data)
        _write_state(data)
        return result


# --------------------------------------------------------------------------
# Dashboard API client (session-cookie auth, port 9119)
#
# The dashboard's own auth gate (hermes_cli.dashboard_auth) does NOT accept
# a plain HTTP Basic Authorization header on gated /api/* routes -- despite
# the credential pair being named HERMES_DASHBOARD_BASIC_AUTH_*, it backs a
# password-login SESSION PROVIDER named "basic". The real flow (confirmed
# live): POST username/password to /auth/password-login, which sets three
# cookies (hermes_session_at/rt/provider); THOSE cookies, not the Basic
# header, are what gated routes check. A handful of routes (dashboard
# themes/plugins, /api/status, /api/health) are genuinely public and don't
# need this at all -- see hermes_cli/dashboard_auth/public_paths.py.
# --------------------------------------------------------------------------

_dash_client = httpx.AsyncClient(timeout=30, base_url=DASHBOARD_BASE)
_dash_login_lock = asyncio.Lock()
_dash_logged_in = False


async def _dashboard_login() -> None:
    global _dash_logged_in
    r = await _dash_client.post(
        "/auth/password-login",
        json={"provider": "basic", "username": DASHBOARD_USER, "password": DASHBOARD_PASS},
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Could not authenticate to the backend: HTTP {r.status_code}")
    _dash_logged_in = True


async def _dashboard_request(method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None) -> httpx.Response:
    global _dash_logged_in
    if not _dash_logged_in:
        async with _dash_login_lock:
            if not _dash_logged_in:
                await _dashboard_login()
    r = await _dash_client.request(method, path, params=params, json=json_body)
    if r.status_code == 401:
        # Access-token cookie expired -- re-login once and retry.
        async with _dash_login_lock:
            _dash_logged_in = False
            await _dashboard_login()
        r = await _dash_client.request(method, path, params=params, json=json_body)
    return r


async def dash_get(path: str, query: Optional[dict] = None, **params) -> Any:
    """query exists alongside **params so a caller can pass a query param
    actually named "path" (e.g. /api/files?path=...) without colliding
    with this function's own first positional argument of the same name --
    dash_get("/api/files", path=x) raises "multiple values for argument
    'path'" instead of doing what it looks like it does. Real bug, found
    live: every Files-page directory beyond root, and every file read,
    500'd because of exactly this."""
    r = await _dashboard_request("GET", path, params={**(query or {}), **params})
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return r.json()


async def dash_send(method: str, path: str, body: Optional[dict] = None, query: Optional[dict] = None) -> Any:
    r = await _dashboard_request(method, path, params=query, json_body=body)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    if not r.content:
        return {}
    return r.json()


# --------------------------------------------------------------------------
# Bot chat -- runs in-process via engine.py instead of over HTTP to a
# separately-running gateway. See engine.py's module docstring for why and
# how; these three functions are thin wrappers that add the one thing
# engine.py deliberately doesn't own -- zBots' local state (which session
# id is each bot's current active one).
#
# Two session operations stay on HTTP for now, not yet ported to engine.py:
# mid-conversation model-switch (set_session_model below) and session
# delete (delete_session further down). Both are narrower, edge-case calls
# outside the core chat path, not the same admin-CRUD-stays-HTTP reasoning
# as profiles/MCP/skills/env/cron -- they could move to engine.py later the
# same way chat/sessions/messages did, just not done in this pass.
# --------------------------------------------------------------------------

# engine.py is loaded relative to this package when main.py is imported as
# backend.main (tests do this, importing from the repo root), and as a bare
# top-level module when uvicorn runs it directly with cwd inside backend/
# (main:app is not part of any package in that context, so the relative
# form raises ImportError there instead).
try:
    from . import engine as _engine
except ImportError:
    import engine as _engine

try:
    from . import bot_processes
except ImportError:
    import bot_processes

try:
    from . import push
except ImportError:
    import push

try:
    from . import persona
except ImportError:
    import persona

# asyncio.create_task()'s own docs warn that a task with no strong
# reference held elsewhere can be garbage-collected mid-flight -- this
# set is that reference, with a done-callback to stop holding it once it
# finishes (success or failure) so the set doesn't grow forever. Same
# pattern supervisor_mcp.py already uses for its own delegated-task
# background sends.
_background_tasks: set = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _reserved_provider_ids() -> frozenset[str]:
    """Built-in provider names hermes-agent's own resolver recognizes
    (deepseek, groq, mistral, zai, ...) -- imported from the real registry
    rather than duplicated here, so it can't drift out of date.

    Real bug found live: a custom endpoint saved with name "deepseek"
    silently got routed through hermes-agent's built-in "deepseek" overlay
    instead of the custom entry's own base_url/key_env -- same slug, and
    the resolver checks PROVIDER_REGISTRY first. Every request then failed
    auth looking for DEEPSEEK_API_KEY, a variable the user never set and
    had no reason to, since the self-service form never asked for it.
    """
    # noqa: PLC0415 -- vendor path only ready once _engine is imported
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.providers import ALIASES

    # These three are NOT in PROVIDER_REGISTRY/ALIASES -- they're routing
    # *modes*, not vendor entries, so they were never added there -- but
    # agent/agent_init.py hardcodes them as a special-cased exclusion set
    # (`_explicit not in {"auto", "openrouter", "custom"}`) that skips the
    # normal "missing API key" fail-fast error entirely. Real bug found
    # live: a custom endpoint saved as "openrouter" hit this exact
    # exclusion -- resolve_provider_client() has its own native OpenRouter
    # path (checks OPENROUTER_API_KEY, ignores the custom entry's key_env
    # entirely) that silently returned nothing, and with the fail-fast
    # branch skipped, the request just fell through to the generic "No LLM
    # provider configured" 500 instead of a clear error either way.
    _hardcoded_reserved = frozenset({"auto", "openrouter", "custom"})

    return frozenset(PROVIDER_REGISTRY.keys()) | frozenset(ALIASES.keys()) | _hardcoded_reserved


def _custom_endpoint_id(raw: str) -> str:
    # Mirrors hermes_cli/web_server.py's own _custom_endpoint_id() slug
    # exactly, so this predicts the real id the dashboard will assign.
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip()).strip("-_").lower()
    return slug or "custom"


def _bot_base(profile: str) -> str:
    """Same reasoning and duplication as engine.py's own _bot_base (kept
    separate to avoid a circular import) -- every bot, "default" included,
    now runs its own dedicated worker process (bot_processes.py), each
    with its own real, unscoped api_server on its own port. This function
    itself never wakes a sleeping worker -- callers that dial a bot
    outside send_to_bot/stream_to_bot's own wrapper (create_bot,
    update_bot, delete_session) call bot_processes.ensure_bot_process_running()
    explicitly first; see each call site's own comment.
    """
    return f"http://127.0.0.1:{bot_processes.get_port(profile)}"


def _api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_SERVER_KEY}", "Content-Type": "application/json"}


def _provision_profile_api_server_key(profile: str) -> None:
    """Give a freshly-created profile its own copy of API_SERVER_KEY.

    Real bug found live: with gateway.multiplex_profiles on (required for
    /p/<profile>/ routing to actually scope to that profile instead of
    silently falling through to "default" -- see entrypoint.sh's own
    config.yaml comment), hermes-agent deliberately treats API_SERVER_KEY
    as a per-profile secret, not a global env var (see vendor's
    agent/secret_scope.py -- "API_SERVER_KEY is deliberately NOT [global]
    -- it IS a credential and stays [profile-scoped]"). A profile with no
    API_SERVER_KEY of its own in its own <profile>/.env can create a
    session (unauthenticated create), but every SUBSEQUENT call through
    its /p/<profile>/ prefix -- including _lock_active_session_model()
    right after this, in the same request -- 401s. Every bot created
    before this fix needed the same line backfilled by hand; this makes
    it automatic going forward. Best-effort: a profile whose directory
    isn't found yet (dash_send above returned before the filesystem
    settled) shouldn't fail bot creation over a secret that already
    exists at the container level for "default" and can be added by hand
    if this silently no-ops.
    """
    home = os.environ.get("HERMES_HOME", "")
    if not home or not API_SERVER_KEY:
        return
    env_path = Path(home) / "profiles" / profile / ".env"
    try:
        if not env_path.exists():
            return
        existing = env_path.read_text(encoding="utf-8")
        if "API_SERVER_KEY" in existing:
            return
        with env_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"API_SERVER_KEY={API_SERVER_KEY}\n")
    except OSError:
        pass


async def _sync_profile_provider(profile: str, provider: Optional[str]) -> None:
    """Give profile's own config.yaml a real definition for `provider`, not
    just a model: reference to a name only the default profile defines.

    Real bug found live (Phase 1 http-transport testing, see the project
    plan): under gateway.multiplex_profiles, each profile is a fully
    independent Hermes install -- vendor's gateway/run.py
    _profile_runtime_scope redirects HERMES_HOME to that profile's own
    directory with NO inheritance from the default profile's config.
    create_bot()/update_bot() have only ever written model: {provider,
    default} into a profile's own config.yaml -- never the providers:
    block that actually defines what that provider IS (base_url,
    key_env, ...), which only the default profile's config has (written
    once by entrypoint.sh's bootstrap, or by the /providers CRUD below,
    both of which only ever touch the default profile).

    This silently worked under the embedded chat transport the whole time
    zBots has existed: engine._get_adapter() builds ONE shared adapter
    from the DEFAULT profile's config and reuses it for every bot
    regardless of profile scope, so provider resolution always resolved
    against that one cached copy no matter which profile a chat call was
    "scoped" to. The real gateway process (http transport) resolves
    providers strictly from whichever profile's config is actually in
    scope for that request -- confirmed live: the instant
    ZBOTS_CHAT_TRANSPORT=http was set, every non-default bot's first chat
    call failed with "Unknown provider '<name>'" (the default bot was
    unaffected -- its "own" config already IS the default profile's).

    Skips built-in routing providers (openrouter, auto, custom, and
    anything hermes-agent's own PROVIDER_REGISTRY/ALIASES recognize --
    see _reserved_provider_ids) -- those resolve through native code paths
    and env-var API keys, not a providers: entry, so there's nothing to
    copy. Best-effort: a provider that isn't in the default profile's own
    config (shouldn't happen -- every provider a bot can be assigned to
    comes from that same catalog, see /models) or a dash_send failure
    here shouldn't fail bot creation/update over a sync step: the bot
    still works fine on the embedded transport either way, and this only
    matters once/if the http transport is enabled.
    """
    if not provider or provider in _reserved_provider_ids():
        return
    try:
        default_cfg = await dash_get("/api/config")
        provider_cfg = (default_cfg.get("providers") or {}).get(provider)
        if not provider_cfg:
            return
        await dash_send(
            "PUT", "/api/config", {"config": {"providers": {provider: provider_cfg}}}, query={"profile": profile}
        )
        _provision_profile_provider_secret(profile, provider_cfg)
    except Exception:
        pass


def _provision_profile_provider_secret(profile: str, provider_cfg: dict) -> None:
    """Give profile's own .env the API key its newly-synced provider needs.

    Same real bug class as _provision_profile_api_server_key, found live
    right after fixing that one and re-testing: syncing the providers:
    block (above) was enough for an unauthenticated endpoint (zBots' own
    self-hosted sglang providers, no key_env at all) but not for anything
    with real credentials -- a synced deepseek-flash entry still 401'd
    with "Authentication Fails ... invalid" the moment a non-default bot
    tried to chat, because key_env (HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY)
    only resolves through the multiplex profile's own .env-backed secret
    scope (agent/secret_scope.py, same mechanism API_SERVER_KEY needed) --
    it is NOT read from this container's process-level environment for a
    scoped profile, confirmed live: the container's own env has no such
    variable at all, only the root/default profile's own .env does.

    Best-effort, same as its sibling: no key_env on this provider (an
    unauthenticated endpoint), the value not found in the default
    profile's own .env, or a filesystem error here shouldn't fail bot
    creation/update -- the bot still works on the embedded transport
    either way.
    """
    key_env = provider_cfg.get("key_env")
    if not key_env:
        return
    home = os.environ.get("HERMES_HOME", "")
    if not home:
        return
    try:
        root_env_path = Path(home) / ".env"
        if not root_env_path.exists():
            return
        value = None
        for line in root_env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key_env}="):
                value = line[len(key_env) + 1 :]
                break
        if value is None:
            return
        target_env_path = Path(home) / "profiles" / profile / ".env"
        if not target_env_path.exists():
            return
        existing = target_env_path.read_text(encoding="utf-8")
        if f"{key_env}=" in existing:
            return
        with target_env_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{key_env}={value}\n")
    except OSError:
        pass


def _is_task_category(state: dict, profile: str) -> bool:
    return (state.get("categories") or {}).get(profile) == "task"


async def send_to_bot(profile: str, message: str, *, timeout: float = 300.0) -> str:
    # A no-op under the embedded transport (no per-bot worker exists at
    # all there -- one shared in-process adapter serves every profile).
    # Under http, this is the wake trigger a sleeping on-demand bot needs:
    # spawns the worker if it isn't already tracked as alive, and blocks
    # until its real GET /health succeeds, before the chat call below ever
    # dials it. See bot_processes.py's own module docstring.
    if _engine._use_http():
        await bot_processes.ensure_bot_process_running(profile)
    state = _read_state()
    # task-category bots are deliberately stateless -- always starting
    # fresh is the whole point (a task bot has no business remembering a
    # previous unrelated ask). Real bug found live: passing
    # active_session_id=None alone does NOT achieve this once the bot has
    # any prior session -- _ensure_bot_chat_session's own fallback reuses
    # the latest one by title-family search, so a second message landed
    # back on the FIRST message's own session (confirmed live: asking a
    # task bot to recall something told to it the message before
    # succeeded, when it should have had no memory of it at all).
    # force_new_session is the real fix -- see its own docstring in
    # engine.py. The old session is never deleted, same convention as a
    # rollover -- it just never gets read back into context again.
    is_task = _is_task_category(state, profile)
    active_id = None if is_task else (state.get("active_sessions") or {}).get(profile)
    reply, session_id = await _engine.send_to_bot(
        profile, message, API_SERVER_KEY, active_id, force_new_session=is_task
    )
    if session_id != active_id:
        await _mutate_state(lambda d: d.setdefault("active_sessions", {}).__setitem__(profile, session_id))
    return reply


async def stream_to_bot(profile: str, message: str):
    """SSE variant of send_to_bot() for the interactive chat UI -- real
    per-token deltas from the engine's own streaming handler, run in-process
    via engine.stream_to_bot() (see its docstring for how a StreamResponse-
    shaped aiohttp handler runs without a real socket, and for the
    session-rollover retry it does internally on a real, common failure
    class). Session bookkeeping mirrors send_to_bot()'s own pattern, with
    one difference forced by streaming: engine.stream_to_bot() can still
    change which session ended up serving the reply mid-stream (a
    rollover), so the final session id is only known once the stream is
    fully drained -- read back from session_state AFTER the loop, not
    before it, unlike send_to_bot()'s single return value.
    """
    if _engine._use_http():
        await bot_processes.ensure_bot_process_running(profile)
    state = _read_state()
    # See send_to_bot's own comment on _is_task_category and
    # force_new_session -- same deliberate statelessness, same real fix,
    # applied to the streaming path.
    is_task = _is_task_category(state, profile)
    active_id = None if is_task else (state.get("active_sessions") or {}).get(profile)
    session_state, chunks = await _engine.stream_to_bot(
        profile, message, API_SERVER_KEY, active_id, force_new_session=is_task
    )
    async for chunk in chunks:
        yield chunk
    final_session_id = session_state["session_id"]
    if final_session_id != active_id:
        await _mutate_state(lambda d: d.setdefault("active_sessions", {}).__setitem__(profile, final_session_id))


async def get_bot_activity(profile: str) -> dict:
    """This bot's own most-recent session summary for the roster (preview/
    timestamp/active) across its whole session family.
    """
    try:
        sessions = await _engine._list_bot_sessions(profile, _engine._api_headers(API_SERVER_KEY))
    except RuntimeError:
        return {}
    if not sessions:
        return {}
    session = sessions[-1]
    last_active = session.get("last_active") or session.get("started_at")
    is_active = session.get("ended_at") is None and last_active is not None and (time.time() - last_active) < 300
    return {
        # strip_context_bridge_note: a rollover's retry becomes the fresh
        # session's own opening message, and this native `preview` field
        # is set from a session's first stored message -- see that
        # function's own docstring for why the recap note needs
        # stripping here specifically (get_bot_messages' own chat view
        # already hides it via dedup; this field bypasses that).
        "preview": _engine.strip_context_bridge_note(session.get("preview") or ""),
        "last_active": last_active,
        "is_active": is_active,
    }


async def get_bot_messages(profile: str, limit: int = 200) -> list[dict]:
    return await _engine.get_bot_messages(profile, API_SERVER_KEY, limit=limit)


# --------------------------------------------------------------------------
# Roster
# --------------------------------------------------------------------------

def _validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Bot name must be lowercase alphanumeric (with - or _), max 64 chars.")
    return name


# Bot lifecycle categories -- see _infer_bot_category()'s own docstring for
# what drives each one and _keep_warm_bots() for how they affect worker
# lifecycle. "general" is the implicit default for any bot with no entry in
# state["categories"] (every bot that existed before this feature).
CATEGORIES = ("chore", "task", "developer", "supervisor", "general")


def _validate_category(category: str) -> str:
    category = (category or "").strip().lower()
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of: {', '.join(CATEGORIES)}")
    return category


class RosterEntry(BaseModel):
    name: str
    title: str
    description: str
    model: Optional[str] = None
    provider: Optional[str] = None
    gateway_running: bool = False
    is_active: bool = False
    is_hidden: bool = False
    preview: str = ""
    last_active: Optional[float] = None
    avatar: dict[str, Any] = {}
    category: str = "general"


@app.get("/roster")
async def get_roster(include_hidden: bool = False) -> list[RosterEntry]:
    profiles_resp = await dash_get("/api/profiles")
    profiles = profiles_resp.get("profiles", []) if isinstance(profiles_resp, dict) else profiles_resp
    state = _read_state()
    hidden = set(state.get("hidden") or [])
    avatars = state.get("avatars") or {}
    titles = state.get("titles") or {}
    locked_models = state.get("locked_models") or {}
    categories = state.get("categories") or {}

    visible = [p for p in profiles if p.get("name") and (include_hidden or p["name"] not in hidden)]
    activity = await asyncio.gather(*(get_bot_activity(p["name"]) for p in visible))

    entries: list[RosterEntry] = []
    for p, latest in zip(visible, activity):
        name = p["name"]
        # Prefer what we actually session-locked (see
        # _lock_active_session_model's docstring) over Hermes' own
        # profile-level provider field, which silently mangles any
        # provider name it doesn't recognize as a built-in type.
        locked = locked_models.get(name) or {}
        entries.append(
            RosterEntry(
                name=name,
                title=p.get("display_name") or titles.get(name) or _pretty_title(name),
                description=p.get("description") or "",
                model=locked.get("model") or p.get("model"),
                provider=locked.get("provider") or p.get("provider"),
                gateway_running=bool(p.get("gateway_running")),
                is_active=bool(latest.get("is_active")),
                is_hidden=name in hidden,
                preview=latest.get("preview") or "",
                last_active=latest.get("last_active"),
                avatar=avatars.get(name) or {"type": "blob"},
                category=categories.get(name) or "general",
            )
        )
    entries.sort(key=lambda e: e.last_active or 0, reverse=True)
    return entries


# --------------------------------------------------------------------------
# Bot CRUD
# --------------------------------------------------------------------------

class BotCreate(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    clone_from: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    soul: Optional[str] = None
    no_skills: bool = False
    # Omitted -> inferred from description (_infer_bot_category); an
    # explicit value here always wins over inference, no LLM call made.
    category: Optional[str] = None


async def _bot_current_provider_model(name: str) -> tuple[Optional[str], Optional[str]]:
    """This bot's actual current (provider, model) -- same resolution order
    as the roster builder (locked session override first, then Hermes'
    own profile record). Used to PIN a routine's provider/model at creation
    time instead of leaving the job unpinned.

    Hermes' own drift guard (cron/scheduler.py, issue #44585) deliberately
    fails an unpinned cron job closed -- no execution, no charge -- the
    moment its creation-time provider/model resolution stops matching the
    CURRENT global default, specifically to stop a routine from silently
    switching to a different (possibly paid) model out from under the
    user. Real, live consequence found for zBots: every routine created
    here left provider/model unset, so any later bot model change (the
    roster's own "change model" action, used all the time) silently broke
    every one of that bot's existing routines -- reported live as "your
    hourly hydration reminder has been failing since the model switch."
    Pinning to whatever the bot is actually running right now sidesteps
    the guard entirely (a pinned axis is never considered drifted) and
    keeps the routine tied to a fixed, known-good target instead of
    "whatever this bot happens to default to at fire time."
    """
    state = _read_state()
    locked = (state.get("locked_models") or {}).get(name) or {}
    if locked.get("provider") and locked.get("model"):
        return locked["provider"], locked["model"]
    profiles_resp = await dash_get("/api/profiles")
    profiles = profiles_resp.get("profiles", []) if isinstance(profiles_resp, dict) else profiles_resp
    for p in profiles:
        if p.get("name") == name:
            return locked.get("provider") or p.get("provider"), locked.get("model") or p.get("model")
    return locked.get("provider"), locked.get("model")


async def _default_model() -> tuple[Optional[str], Optional[str]]:
    """The 'default' profile's own (provider, model), to fall back on.

    A bot with no explicit provider/model in its own config.yaml (i.e. one
    created via clone_from=None with no override) hits an intermittent
    Hermes-side routing fallback -- confirmed live: the same untouched bot
    resolved to "qwen-rtx5090/qwen3-agent:latest" on one chat call and to an
    uncredentialed "custom/main" on the very next, causing a 500. Always
    setting an explicit provider/model at creation time avoids that
    ambiguity entirely rather than relying on inheritance.
    """
    profiles_resp = await dash_get("/api/profiles")
    profiles = profiles_resp.get("profiles", []) if isinstance(profiles_resp, dict) else profiles_resp
    for p in profiles:
        if p.get("name") == "default":
            return p.get("provider"), p.get("model")
    return None, None


_CATEGORY_DESCRIPTIONS = {
    "chore": "recurring, scheduled/periodic work -- reminders, daily check-ins, anything meant to fire on a timer",
    "task": "small, one-off requests with no need to remember earlier unrelated asks -- quick lookups, one-shot conversions",
    "developer": "long-running autonomous work -- coding, multi-step builds, anything that may run unattended for a long time and needs to keep full context throughout",
    "supervisor": "reviews/evaluates other bots' recent work and reports on it -- a QC/oversight role, not a task-doer itself",
    "general": "a normal conversational assistant that doesn't clearly fit the above",
}

_CATEGORY_PROMPT_TEMPLATE = (
    "Classify a bot by its description into EXACTLY ONE of these categories:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in _CATEGORY_DESCRIPTIONS.items())
    + "\n\nBot description: {description}\n\n"
    "Reply with ONLY the single category word (chore, task, developer, supervisor, or general) -- no punctuation, no explanation."
)


async def _infer_bot_category(description: str) -> str:
    """Classify a new bot's lifecycle category from its own description --
    the user's own explicit instruction: "it should be inferred [from the
    description]... and the user can manually change its type as well
    later if required." See CATEGORIES/_keep_warm_bots for what each
    value actually changes about the bot's lifecycle.

    Runs the classification as a real chat turn on a TEMPORARY session on
    "default" (create, one turn, delete) rather than default's own
    visible "[Bots UI] default" thread -- _bot_session_rollover_n's own
    title-family matching means a differently-titled session is never
    picked up by the merged-history view, so this never pollutes
    default's real chat with a classification exchange the user never
    asked to see.

    Best-effort throughout: an empty description skips the LLM call
    entirely (nothing to classify); a reply that isn't exactly one of the
    five known category words (a hedge, an explanation despite the
    instruction not to, or any failure -- unreachable worker, timeout,
    the temp session itself failing to create) falls back to "general"
    rather than failing bot creation over a classification miss. Cleanup
    (deleting the temp session) is ALSO best-effort -- a failed delete
    just leaves one harmless orphaned session, never worth failing
    creation over.
    """
    description = (description or "").strip()
    if not description:
        return "general"
    headers = _engine._api_headers(API_SERVER_KEY)
    session_id = None
    try:
        await bot_processes.ensure_bot_process_running("default")
        session_id = await _engine._create_bot_session("default", "[zBots category inference]", headers)
        prompt = _CATEGORY_PROMPT_TEMPLATE.format(description=description)
        with _engine._profile_scope("default"):
            status, body = await _engine._call_handler(
                "_handle_session_chat",
                profile="default",
                method="POST",
                path=f"/api/sessions/{session_id}/chat",
                json_body={"message": prompt},
                headers=headers,
                match_info={"session_id": session_id},
            )
        if status >= 400:
            return "general"
        msg = (body or {}).get("message")
        reply = str(msg.get("content") or "") if isinstance(msg, dict) else ""
        category = reply.strip().lower().strip(".!\"'")
        return category if category in CATEGORIES else "general"
    except Exception:
        return "general"
    finally:
        if session_id:
            try:
                base = _bot_base("default")
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.delete(f"{base}/api/sessions/{session_id}", headers=_api_headers())
            except Exception:
                pass


@app.post("/bots")
async def create_bot(body: BotCreate) -> RosterEntry:
    name = _validate_name(body.name)
    provider, model = body.provider, body.model
    if not provider or not model:
        default_provider, default_model = await _default_model()
        provider = provider or default_provider
        model = model or default_model
    await dash_send(
        "POST",
        "/api/profiles",
        {
            "name": name,
            "clone_from": body.clone_from,
            "description": body.description,
            "provider": provider,
            "model": model,
            "no_skills": body.no_skills,
        },
    )
    _provision_profile_api_server_key(name)
    await _sync_profile_provider(name, provider)
    if body.title:
        await _mutate_state(lambda d: d["titles"].__setitem__(name, body.title))
    category = _validate_category(body.category) if body.category else await _infer_bot_category(body.description)
    await _mutate_state(lambda d: d["categories"].__setitem__(name, category))
    if body.soul:
        await dash_send("PUT", f"/api/profiles/{name}/soul", {"content": body.soul})
    _engine.invalidate_adapter()
    if provider and model:
        # Real bug found live: Hermes' own profile-level provider storage
        # silently coerces any provider name it doesn't recognize as one
        # of its built-in TYPEs (e.g. this platform's custom OpenAI-
        # compatible endpoints like "hermes4-bitbots") down to
        # "openrouter" -- confirmed by creating a bot and reading its
        # profile back: requested "hermes4-bitbots" landed as
        # "openrouter", an unconfigured provider with no API key, making
        # the new bot appear broken/unresponsive despite creation itself
        # reporting success. The SESSION-level model lock
        # (POST /api/sessions/{id}/model, same endpoint
        # _lock_active_session_model() already uses for existing bots)
        # does NOT have this bug. So give the new bot its own canonical
        # session immediately and lock the real provider onto it here,
        # rather than trusting the profile field to hold it correctly.
        try:
            # A brand-new bot's worker has never been spawned -- both
            # calls below always do real HTTP against this bot's own
            # worker (bot_processes.py), regardless of engine.py's own
            # ZBOTS_CHAT_TRANSPORT, so it has to exist first either way.
            await bot_processes.ensure_bot_process_running(name)
            await _engine._ensure_bot_chat_session(name, _engine._api_headers(API_SERVER_KEY), None)
            await _lock_active_session_model(name, provider, model)
        except Exception:
            pass  # best-effort -- profile creation above already succeeded
    roster = await get_roster(include_hidden=True)
    for entry in roster:
        if entry.name == name:
            return entry
    raise HTTPException(status_code=500, detail="Bot created but not found in roster afterward.")


@app.get("/bots/{name}/soul")
async def get_bot_soul(name: str) -> dict:
    """So the edit modal can show what's actually there instead of a blank
    textarea -- previously write-only, forcing a user who wanted to tweak
    one line of an existing soul to retype the whole thing from memory."""
    name = _validate_name(name)
    return await dash_get(f"/api/profiles/{name}/soul")


class BotUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    soul: Optional[str] = None
    # Manual override -- the user's own explicit instruction: inferred at
    # creation, but "the user can manually change its type as well later
    # if required." No re-inference call here; this is a direct
    # statement, not a request to reclassify.
    category: Optional[str] = None


@app.patch("/bots/{name}")
async def update_bot(name: str, body: BotUpdate) -> dict:
    name = _validate_name(name)
    if body.title is not None:
        if name == "default":
            # The only profile Hermes itself supports a real display-name
            # rename for -- use its native endpoint so it stays consistent
            # with the CLI/desktop's own concept of "default"'s title.
            await dash_send("PATCH", f"/api/profiles/{name}", {"new_name": body.title})
        else:
            await _mutate_state(lambda d: d["titles"].__setitem__(name, body.title))
    if body.category is not None:
        category = _validate_category(body.category)
        await _mutate_state(lambda d: d["categories"].__setitem__(name, category))
    if body.description is not None:
        await dash_send("PUT", f"/api/profiles/{name}/description", {"description": body.description})
    if body.provider and body.model:
        await dash_send("PUT", f"/api/profiles/{name}/model", {"provider": body.provider, "model": body.model})
        await _sync_profile_provider(name, body.provider)
        try:
            # _lock_active_session_model always does real HTTP against
            # this bot's own worker (bot_processes.py) -- a currently-
            # sleeping on-demand bot needs waking first, or its own POST
            # (already best-effort/fails-soft internally) would silently
            # no-op against a port nothing is listening on.
            await bot_processes.ensure_bot_process_running(name)
        except Exception:
            pass
        await _lock_active_session_model(name, body.provider, body.model)
    if body.soul is not None:
        await dash_send("PUT", f"/api/profiles/{name}/soul", {"content": body.soul})
    if body.description is not None or body.soul is not None or (body.provider and body.model):
        # Real bug found live: a soul/model/description change written
        # through the dashboard API above takes effect immediately for
        # anything that reads the profile fresh (the roster, the CLI),
        # but the embedded chat engine kept answering with the
        # pre-mutation persona until the whole backend process was
        # restarted -- confirmed with a controlled test (same fresh
        # session either way; only a process restart changed the
        # outcome). See engine.invalidate_adapter()'s own docstring.
        _engine.invalidate_adapter()
    return {"ok": True}


async def _lock_active_session_model(profile: str, provider: str, model: str) -> None:
    """Changing a profile's default model only affects brand-new sessions.
    Hermes pins a session's model to whatever it resolved on its first turn
    (api_server.py's _stored_session_model reads session["model"], not the
    live profile config) and our own chat calls never pass an explicit
    override, so an already-open conversation would otherwise keep
    answering with the old model until it happens to roll over -- verified
    live: switching a bot's model here left its current session's stored
    model untouched. Hermes has a real endpoint for exactly this,
    POST /api/sessions/{id}/model, described in its own source as
    "backend-ack a Browser model lock" -- call it so switching model from
    the chat header actually takes effect on the conversation you're
    looking at, not just the next one. Best-effort: the profile default
    above has already been saved either way, so a failure here (e.g. no
    session yet) shouldn't fail the whole request.

    Also records (provider, model) into state["locked_models"][profile] on
    success -- real bug found live: Hermes' PROFILE-level provider field
    silently coerces any provider name it doesn't recognize as a built-in
    TYPE (e.g. this platform's custom OpenAI-compatible endpoints like
    "hermes4-bitbots") down to "openrouter", so a bot actually running on
    the right model still shows the wrong one in the roster if that field
    is trusted. This SESSION-level lock doesn't have that bug -- confirmed
    live by reading the lock response back. get_roster() prefers this
    recorded value over the profile's own (unreliable) field.
    """
    state = _read_state()
    session_id = (state.get("active_sessions") or {}).get(profile)
    if not session_id:
        return
    base = _bot_base(profile)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base}/api/sessions/{session_id}/model",
                json={"provider": provider, "model": model},
                headers=_api_headers(),
            )
        if resp.status_code < 400:
            await _mutate_state(
                lambda d: d.setdefault("locked_models", {}).__setitem__(
                    profile, {"provider": provider, "model": model}
                )
            )
    except httpx.HTTPError:
        pass


class DuplicateRequest(BaseModel):
    new_name: str


@app.post("/bots/{name}/duplicate")
async def duplicate_bot(name: str, body: DuplicateRequest) -> RosterEntry:
    name = _validate_name(name)
    new_name = _validate_name(body.new_name)
    await dash_send("POST", "/api/profiles", {"name": new_name, "clone_from": name})
    roster = await get_roster(include_hidden=True)
    for entry in roster:
        if entry.name == new_name:
            return entry
    raise HTTPException(status_code=500, detail="Bot duplicated but not found in roster afterward.")


@app.delete("/bots/{name}")
async def delete_bot(name: str) -> dict:
    name = _validate_name(name)
    await dash_send("DELETE", f"/api/profiles/{name}", None)

    def _cleanup(d):
        if name in d["hidden"]:
            d["hidden"].remove(name)
        d["avatars"].pop(name, None)
        d["titles"].pop(name, None)
        d["active_sessions"].pop(name, None)

    await _mutate_state(_cleanup)
    return {"ok": True}


@app.post("/bots/{name}/hide")
async def hide_bot(name: str) -> dict:
    name = _validate_name(name)
    await _mutate_state(lambda d: d["hidden"].append(name) if name not in d["hidden"] else None)
    return {"ok": True}


@app.post("/bots/{name}/unhide")
async def unhide_bot(name: str) -> dict:
    name = _validate_name(name)
    await _mutate_state(lambda d: d["hidden"].remove(name) if name in d["hidden"] else None)
    return {"ok": True}


# --------------------------------------------------------------------------
# Avatars
# --------------------------------------------------------------------------

class AvatarChoice(BaseModel):
    type: str  # "blob" | "geometric" | "upload"
    seed: Optional[int] = None


@app.put("/bots/{name}/avatar")
async def set_avatar(name: str, body: AvatarChoice) -> dict:
    name = _validate_name(name)
    if body.type not in ("blob", "geometric", "upload"):
        raise HTTPException(status_code=400, detail="type must be blob, geometric, or upload")
    await _mutate_state(lambda d: d["avatars"].__setitem__(name, {"type": body.type, "seed": body.seed}))
    return {"ok": True}


@app.post("/bots/{name}/avatar/upload")
async def upload_avatar(name: str, file: UploadFile = File(...)) -> dict:
    name = _validate_name(name)
    ext = ".png" if not file.filename or "." not in file.filename else "." + file.filename.rsplit(".", 1)[-1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise HTTPException(status_code=400, detail="Unsupported image type")
    dest = AVATAR_DIR / f"{name}{ext}"
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Avatar image too large (max 5MB)")
    dest.write_bytes(data)
    url = f"/bots-avatars/{name}{ext}"
    await _mutate_state(lambda d: d["avatars"].__setitem__(name, {"type": "upload", "url": url}))
    return {"ok": True, "url": url}


# --------------------------------------------------------------------------
# Bot chat
# --------------------------------------------------------------------------

class SendMessage(BaseModel):
    text: str
    # Real gap found live: a routine's own delivery (a cron-triggered turn
    # calling its message_bot tool -- see supervisor_mcp.py's own calls,
    # which set this True) reaches the SAME endpoint the interactive UI
    # calls when the user is live in that bot's chat. The UI never sets
    # this (the user is already looking at the reply as it streams in);
    # an asynchronous delivery the user isn't necessarily watching does,
    # so it can trigger a real push notification -- see push.py's own
    # module docstring for why that needs to be real Web Push, not a
    # same-tab-only Notification() call.
    notify: bool = False


@app.get("/bots/{name}/messages")
async def bot_messages(name: str) -> list[dict]:
    name = _validate_name(name)
    return await get_bot_messages(name)


@app.post("/bots/{name}/wake")
async def bot_wake(name: str) -> dict:
    """Opportunistic pre-warm -- fired by the frontend the moment a bot's
    chat is opened (see selectBot() in app.js), in parallel with loading
    its message history, so the worker is already spinning up while the
    user is still reading/typing instead of only starting on first send.
    Purely latency-hiding: a real chat message still calls
    ensure_bot_process_running() itself on its own path, so a failure or
    race here is never something the user needs to see or retry.
    """
    name = _validate_name(name)
    try:
        await bot_processes.ensure_bot_process_running(name)
    except Exception:
        pass
    return {"ok": True}


@app.post("/bots/{name}/messages")
async def bot_send(name: str, body: SendMessage) -> dict:
    name = _validate_name(name)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text required.")
    reply = await send_to_bot(name, text)
    if body.notify and reply:
        # Fire-and-forget, same convention as supervisor_mcp.py's own
        # background delegated-task pattern -- a slow/failing push send
        # must never add latency to (or fail) the chat reply itself,
        # which has already fully succeeded by this point.
        _fire_and_forget(push.send_push_notification(_pretty_title(name), reply))
    return {"reply": reply}


@app.post("/bots/{name}/messages/stream")
async def bot_send_stream(name: str, body: SendMessage) -> StreamingResponse:
    name = _validate_name(name)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text required.")
    return StreamingResponse(stream_to_bot(name, text), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Push notifications -- see push.py's own module docstring for why real
# Web Push (not a same-tab Notification() call) and why nothing in
# hermes-agent itself could be reused for this.
# --------------------------------------------------------------------------

@app.get("/push/vapid-public-key")
async def push_vapid_public_key() -> dict:
    return {"key": push.get_public_key_b64()}


class PushSubscribe(BaseModel):
    subscription: dict


@app.post("/push/subscribe")
async def push_subscribe(body: PushSubscribe) -> dict:
    try:
        push.add_subscription(body.subscription)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


class PushUnsubscribe(BaseModel):
    endpoint: str


@app.post("/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubscribe) -> dict:
    push.remove_subscription(body.endpoint)
    return {"ok": True}


# --------------------------------------------------------------------------
# Groups
#
# Docs describe the desktop's group chat as up to 3 serial rounds of member
# turns, bots addressing each other with @name or escalating with @user, a
# hard cap of 10 messages per send / 3 rounds. That behavior lives inside
# Hermes' own closed-source composer/agent loop -- there's no API to drive
# it directly. What follows is this companion app's own, simpler
# implementation of the same idea against the real send_to_bot() primitive:
# round 1 fans the user's message out to every @-mentioned bot (or every
# member, if none mentioned); if a reply itself @-mentions another member,
# that member gets pulled into round 2 with the reply as context; round 3
# repeats once more. The 10-message and 3-round caps are enforced exactly.
# --------------------------------------------------------------------------

class GroupCreate(BaseModel):
    name: str
    members: list[str]


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    members: Optional[list[str]] = None


@app.get("/groups")
async def list_groups() -> list[dict]:
    state = _read_state()
    return list((state.get("groups") or {}).values())


@app.post("/groups")
async def create_group(body: GroupCreate) -> dict:
    members = [_validate_name(m) for m in body.members]
    if len(members) < 2:
        raise HTTPException(status_code=400, detail="A group needs at least 2 members.")
    group_id = uuid.uuid4().hex[:12]
    group = {"id": group_id, "name": body.name or f"Group ({', '.join(members)})", "members": members, "messages": []}
    await _mutate_state(lambda d: d["groups"].__setitem__(group_id, group))
    return group


@app.patch("/groups/{group_id}")
async def update_group(group_id: str, body: GroupUpdate) -> dict:
    """Rename a group and/or change its members. Name edits stay local to
    this app's state file -- there is no Hermes-side group object to update.
    """
    def _update(d):
        group = (d.get("groups") or {}).get(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="Group not found.")
        if body.name is not None:
            group["name"] = body.name.strip() or group["name"]
        if body.members is not None:
            members = [_validate_name(m) for m in body.members]
            if len(members) < 2:
                raise HTTPException(status_code=400, detail="A group needs at least 2 members.")
            group["members"] = members
        return group

    return await _mutate_state(_update)


@app.delete("/groups/{group_id}")
async def delete_group(group_id: str) -> dict:
    await _mutate_state(lambda d: d["groups"].pop(group_id, None))
    return {"ok": True}


@app.get("/groups/{group_id}/messages")
async def group_messages(group_id: str) -> list[dict]:
    state = _read_state()
    group = (state.get("groups") or {}).get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found.")
    return group.get("messages") or []


class GroupSend(BaseModel):
    text: str
    sender: str = "user"


async def _append_group_message(group_id: str, entry: dict) -> None:
    def _do(d):
        group = (d.get("groups") or {}).get(group_id)
        if group is not None:
            group.setdefault("messages", []).append(entry)

    await _mutate_state(_do)


def _group_turn_context(group: dict, transcript: list[tuple[str, str]], round_num: int) -> str:
    """Build the prompt one member sees for one turn of a group send.

    Round 1 is the user's message on its own, exactly as before. Later rounds
    include the user's message plus every reply already posted in this send,
    so a bot that is pulled in by an @mention actually sees the conversation
    it is being asked to continue, not a detached copy of the first message.
    """
    lines = [f"(in group '{group['name']}')"]
    if round_num == 0:
        lines.append(transcript[0][1])
    else:
        for sender, text in transcript:
            lines.append(f"{sender}: {text}" if sender != "user" else text)
    return "\n".join(lines)


@app.post("/groups/{group_id}/messages")
async def group_send(group_id: str, body: GroupSend) -> dict:
    state = _read_state()
    group = (state.get("groups") or {}).get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found.")
    members = group.get("members") or []

    user_entry = {"from": "user", "text": body.text, "ts": time.time()}
    await _append_group_message(group_id, user_entry)

    sent = 0
    round_targets = [m for m in _MENTION_RE.findall(body.text) if m in members] or list(members)
    posted: list[dict] = []
    # Running transcript of THIS send, used as context for later rounds so a
    # reply that @-mentions another member is answered with the actual
    # previous replies in front of it, not just the original user message.
    transcript: list[tuple[str, str]] = [("user", body.text)]
    for round_num in range(GROUP_MAX_ROUNDS):
        if not round_targets or sent >= GROUP_MAX_MESSAGES:
            break
        next_targets: set[str] = set()
        for target in round_targets:
            if sent >= GROUP_MAX_MESSAGES:
                break
            context = _group_turn_context(group, transcript, round_num)
            try:
                reply = await send_to_bot(target, context)
            except HTTPException as exc:
                reply = f"[error contacting {target}: {exc.detail}]"
            entry = {"from": target, "text": reply, "ts": time.time(), "round": round_num + 1}
            await _append_group_message(group_id, entry)
            posted.append(entry)
            sent += 1
            transcript.append((target, reply))
            for mention in _MENTION_RE.findall(reply):
                if mention in members and mention != target:
                    next_targets.add(mention)
        round_targets = list(next_targets)

    return {"ok": True, "messages": posted}


# --------------------------------------------------------------------------
# Routines (real hermes cron jobs, namespaced "[bot:<name>] <routine>")
# --------------------------------------------------------------------------

def _routine_job_name(bot: str, routine: str, target: Optional[str] = None) -> str:
    # target != bot is a cross-bot nudge (this bot's routine delivers into
    # ANOTHER bot's chat) -- recorded in the name itself since "deliver"
    # stays "local" for every routine now (see create_routine's own
    # comment for why), so the name is the only place this survives to be
    # read back for display.
    if target and target != bot:
        return f"[bot:{bot}->{target}] {routine}"
    return f"[bot:{bot}] {routine}"


_ROUTINE_NAME_RE = re.compile(r"^\[bot:([a-zA-Z0-9_-]+)(?:->([a-zA-Z0-9_-]+))?\]")


def _routine_bot_from_job(job: dict) -> Optional[str]:
    m = _ROUTINE_NAME_RE.match(job.get("name") or "")
    return m.group(1) if m else None


def _routine_target_from_job(job: dict) -> Optional[str]:
    m = _ROUTINE_NAME_RE.match(job.get("name") or "")
    return m.group(2) if m else None


@app.get("/bots/{name}/routines")
async def list_routines(name: str) -> list[dict]:
    name = _validate_name(name)
    jobs = await dash_get("/api/cron/jobs", profile="all")
    rows = jobs if isinstance(jobs, list) else jobs.get("data", [])
    out = []
    for j in rows:
        if _routine_bot_from_job(j) != name:
            continue
        j = dict(j)
        j["target_bot"] = _routine_target_from_job(j)
        out.append(j)
    return out


class RoutineCreate(BaseModel):
    routine: str  # short label, becomes part of the job name
    prompt: str
    schedule: str  # raw hermes cron schedule string
    target_bot: Optional[str] = None  # deliver into ANOTHER bot's chat instead of this one's


@app.post("/bots/{name}/routines")
async def create_routine(name: str, body: RoutineCreate) -> dict:
    name = _validate_name(name)
    target = _validate_name(body.target_bot) if body.target_bot else name
    provider, model = await _bot_current_provider_model(name)
    # Delivery: Hermes' own native "bot-chat" deliver target looked like the
    # obvious choice (it's built for exactly this -- posting a job's output
    # as an inbound turn on a profile's chat), and DOES work for RECURRING
    # jobs. For a FINITE one-shot ("30m", "1m", etc.) it doesn't: confirmed
    # live, twice, with zero log trace either time -- the job fires, and
    # instead of landing in state=completed (like an identical job with
    # deliver=local does) it's deleted outright from the store, so nothing
    # ever reaches the chat and there's nothing left to even diagnose.
    # bot-chat's own delivery path spawns a SEPARATE `hermes chat` CLI
    # subprocess against the same HERMES_HOME, which runs its own
    # gateway-boot-style reconcile on start -- the leading theory, not yet
    # proven, is that reconcile treats the just-fired one-shot as an orphan
    # and prunes it out from under the parent process before the normal
    # one-shot completion bookkeeping runs.
    #
    # Used instead: the SAME mechanism this gateway's own pre-existing
    # agentic cron jobs (bobby-checkin, hydration-reminder, ...) already use
    # successfully today -- confirmed live, both before and after this fix,
    # real chat turns landing in the target bot's actual visible history.
    # The prompt itself instructs the model to call its message_bot tool
    # (backend/supervisor_mcp.py, already registered for every profile) to
    # deliver the result, so "deliver" stays "local": the delivery already
    # happened as a tool call inside the turn, and auto-delivering the raw
    # turn output on top of that would just double-post it.
    wrapped_prompt = (
        f"{body.prompt}\n\n"
        f"When you're done, use your message_bot tool to send bot name "
        f"{target} the result as a short, friendly chat message -- not a "
        f"raw dump of your reasoning."
    )
    return await dash_send(
        "POST",
        f"/api/cron/jobs?profile={name}",
        {
            "name": _routine_job_name(name, body.routine, target),
            "prompt": wrapped_prompt,
            "schedule": body.schedule,
            "deliver": "local",
            # Pinned so a later model switch on this bot can never trip
            # Hermes' own provider/model drift guard (cron/scheduler.py,
            # #44585), which fails an unpinned job closed -- no run, no
            # charge -- the moment its creation-time resolution stops
            # matching the current default. See
            # _bot_current_provider_model's docstring for the live bug
            # this caused (a routine silently going dead on a model
            # switch, reported as "your hourly reminder has been failing
            # since the model switch").
            "provider": provider,
            "model": model,
        },
    )


@app.delete("/routines/{job_id}")
async def delete_routine(job_id: str) -> dict:
    return await dash_send("DELETE", f"/api/cron/jobs/{job_id}", None)


@app.post("/routines/{job_id}/pause")
async def pause_routine(job_id: str) -> dict:
    return await dash_send("POST", f"/api/cron/jobs/{job_id}/pause", None)


@app.post("/routines/{job_id}/resume")
async def resume_routine(job_id: str) -> dict:
    return await dash_send("POST", f"/api/cron/jobs/{job_id}/resume", None)


# --------------------------------------------------------------------------
# Providers & models
# --------------------------------------------------------------------------

def _clean_model_names(provider_cfg: dict) -> list[str]:
    """Extract plain model-name strings from a providers.<id> config entry.

    Handles both shapes seen in the wild: a bare string list, and this
    deployment's actual shape, a list of {name: "..."} dicts.
    """
    out = []
    for model in provider_cfg.get("models") or []:
        name = model.get("name") if isinstance(model, dict) else model
        if name:
            out.append(str(name))
    return out


@app.get("/providers")
async def list_providers() -> dict:
    """Configured providers + their models, models catalog merged from two
    real endpoints because neither is complete/correct alone:
      - /api/providers/custom-endpoints gives id/is_current/has_api_key, but
        its own "model"/"models" fields come back as a stringified Python
        dict repr for any provider whose models are the {name: "..."} dict
        shape (confirmed live: "model": "{'name': 'qwen3-agent:latest'}") --
        a real bug in that endpoint, not something to paper over client-side.
      - /api/config's providers.<id>.models is the accurate source for
        actual model names (same parsing this app's own /models endpoint
        already does correctly).
    """
    endpoints_resp, cfg = await asyncio.gather(
        dash_get("/api/providers/custom-endpoints"),
        dash_get("/api/config"),
    )
    clean_models = {pid: _clean_model_names(pcfg) for pid, pcfg in (cfg.get("providers") or {}).items()}
    providers = []
    for ep in endpoints_resp.get("endpoints") or []:
        pid = ep.get("id")
        providers.append({
            "id": pid,
            "name": ep.get("name") or pid,
            "base_url": ep.get("base_url") or "",
            "models": clean_models.get(pid) or [m for m in ep.get("models") or [] if not m.startswith("{")],
            "context_length": ep.get("context_length"),
            "has_api_key": bool(ep.get("has_api_key")),
            "is_current": bool(ep.get("is_current")),
        })
    return {"providers": providers, "current": endpoints_resp.get("current") or {}}


class ProviderSave(BaseModel):
    id: str = ""
    name: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    context_length: Optional[int] = None
    discover_models: bool = True
    make_default: bool = False


@app.post("/providers")
async def save_provider(body: ProviderSave) -> dict:
    slug = _custom_endpoint_id(body.id or body.name)
    if slug in _reserved_provider_ids():
        raise HTTPException(
            status_code=400,
            detail=(
                f'"{body.name}" is already a built-in provider name hermes-agent '
                f"recognizes, so it would take over that provider's own auth "
                f"instead of using your endpoint's. Pick a different name, e.g. "
                f'"{body.name}-custom".'
            ),
        )
    result = await dash_send("POST", "/api/providers/custom-endpoints", body.model_dump())
    # Same real bug as create_bot/update_bot's own soul/model edits (see
    # invalidate_adapter's docstring): a provider added or edited here
    # writes to disk correctly, but the embedded chat engine keeps using
    # its already-cached view until this fires.
    _engine.invalidate_adapter()
    return result


@app.post("/providers/{provider_id}/activate")
async def activate_provider(provider_id: str) -> dict:
    result = await dash_send("POST", f"/api/providers/custom-endpoints/{provider_id}/activate", None)
    _engine.invalidate_adapter()
    return result


class ModelActivate(BaseModel):
    provider: str
    model: str


@app.post("/models/activate")
async def activate_model(body: ModelActivate) -> dict:
    """Set one specific model (not necessarily a provider's own default) as
    the active main-slot model -- distinct from /providers/{id}/activate,
    which always uses whatever model the provider entry itself points at.
    """
    result = await dash_send("POST", "/api/model/set", {"scope": "main", "provider": body.provider, "model": body.model, "task": ""})
    _engine.invalidate_adapter()
    return result


@app.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    result = await dash_send("DELETE", f"/api/providers/custom-endpoints/{provider_id}", None)
    _engine.invalidate_adapter()
    return result


class ProviderValidate(BaseModel):
    name: str = "test"
    base_url: str
    model: str = ""
    api_key: Optional[str] = None


@app.post("/providers/validate")
async def validate_provider(body: ProviderValidate) -> dict:
    payload = body.model_dump()
    payload["id"] = ""
    return await dash_send("POST", "/api/providers/custom-endpoints/validate", payload)


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------

@app.get("/models")
async def list_models() -> dict:
    """Provider/model catalog for the create-bot dialog, sourced from the
    main profile's own config.yaml (the same providers every bot inherits
    unless overridden)."""
    cfg = await dash_get("/api/config")
    providers = cfg.get("providers") or {}
    out = []
    for provider_name, provider_cfg in providers.items():
        for model_name in _clean_model_names(provider_cfg):
            out.append({"provider": provider_name, "model": model_name})
    return {"models": out}


# --------------------------------------------------------------------------
# Connectors (Discord/Telegram/WhatsApp/Slack/...) -- thin proxy over
# hermes-agent's own real messaging-platform API (GET/PUT
# /api/messaging/platforms[/{id}]), the same native mechanism the desktop
# app's own Channels page uses. zBots adds nothing here beyond auth/shape --
# the platform catalog (every id in gateway.config.Platform, plus any
# installed plugin platform), credential storage (.env-backed, same
# convention as a custom provider's key_env), and connect/test logic are
# all real, already-built hermes-agent code.
# --------------------------------------------------------------------------

class ConnectorUpdate(BaseModel):
    enabled: Optional[bool] = None
    env: dict[str, str] = {}
    clear_env: list[str] = []


@app.get("/connectors")
async def list_connectors() -> Any:
    # scrub_branding_deep: hermes-agent's own native platform metadata
    # (description/prompt/help text) carries its own branding verbatim --
    # see persona.py's own comment on this function for how that was
    # found and why docs_url is deliberately left untouched.
    return persona.scrub_branding_deep(await dash_get("/api/messaging/platforms"))


@app.put("/connectors/{platform_id}")
async def update_connector(platform_id: str, body: ConnectorUpdate) -> dict:
    result = await dash_send("PUT", f"/api/messaging/platforms/{platform_id}", body.model_dump())
    # Enabling/disabling a platform is gateway-process config (hermes
    # serve's own startup sequence spins up each enabled platform's
    # connector), not the embedded chat engine's -- unlike a provider/model
    # change, this has nothing for invalidate_adapter() to reset. The real
    # API's own "pending_restart" state (surfaced in the response) already
    # tells the caller a hermes-serve restart is what's actually needed.
    return result


@app.post("/connectors/{platform_id}/test")
async def test_connector(platform_id: str) -> dict:
    return await dash_send("POST", f"/api/messaging/platforms/{platform_id}/test", None)


@app.get("/peers")
async def list_peers() -> dict:
    cfg = await dash_get("/api/config")
    peers = cfg.get("bot_peers") or {}
    return {"peers": [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in peers.items()]}


# --------------------------------------------------------------------------
# Sessions -- thin proxy over the real dashboard sessions API
# --------------------------------------------------------------------------

@app.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0) -> Any:
    return await dash_get("/api/profiles/sessions", limit=limit, offset=offset, min_messages=0, archived="exclude")


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, profile: str = "default") -> dict:
    """The dashboard (port 9119) has no session-delete route at all --
    confirmed live, grepped the whole web_server.py/web_routers tree.
    Session deletion only exists on the api_server, the same one bot chat
    already uses -- always real HTTP against that bot's own worker
    (bot_processes.py), independent of engine.py's own
    ZBOTS_CHAT_TRANSPORT (this function never went through the embedded/
    mocked-request path even before that flag existed).
    """
    await bot_processes.ensure_bot_process_running(profile)
    base = _bot_base(profile)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(f"{base}/api/sessions/{session_id}", headers=_api_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return r.json() if r.content else {}


# --------------------------------------------------------------------------
# MCP servers
# --------------------------------------------------------------------------

@app.get("/mcp/servers")
async def list_mcp_servers() -> Any:
    return await dash_get("/api/mcp/servers")


class McpServerCreate(BaseModel):
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: list[str] = []
    env: dict[str, str] = {}
    auth: Optional[str] = None
    bearer_token: Optional[str] = None


@app.post("/mcp/servers")
async def create_mcp_server(body: McpServerCreate) -> dict:
    return await dash_send("POST", "/api/mcp/servers", body.model_dump())


@app.delete("/mcp/servers/{name}")
async def delete_mcp_server(name: str) -> dict:
    return await dash_send("DELETE", f"/api/mcp/servers/{name}", None)


@app.post("/mcp/servers/{name}/test")
async def test_mcp_server(name: str) -> dict:
    return await dash_send("POST", f"/api/mcp/servers/{name}/test", None)


class ToggleBody(BaseModel):
    enabled: bool


@app.put("/mcp/servers/{name}/enabled")
async def set_mcp_server_enabled(name: str, body: ToggleBody) -> dict:
    return await dash_send("PUT", f"/api/mcp/servers/{name}/enabled", body.model_dump())


# --------------------------------------------------------------------------
# MCP catalog -- the "Nous-approved" list of known integrations Hermes
# ships with (GET /api/mcp/catalog, POST /api/mcp/catalog/install), the
# same one the real desktop app's one-click "Add integration" uses. zBots'
# own /mcp/servers above is a manual freeform add-a-server form; this is
# the curated browse-and-click alternative for common tools (GitHub,
# Notion, Figma, Slack, etc.) without hand-typing transport/URL details.
# --------------------------------------------------------------------------

@app.get("/mcp/catalog")
async def get_mcp_catalog() -> Any:
    # See list_connectors' own comment on scrub_branding_deep -- same real
    # bug, same fix (the catalog's own per-integration setup text is the
    # single biggest source of it: 46 mentions across this one response).
    return persona.scrub_branding_deep(await dash_get("/api/mcp/catalog"))


class CatalogInstall(BaseModel):
    name: str
    env: dict[str, str] = {}
    enable: bool = True


@app.post("/mcp/catalog/install")
async def install_mcp_catalog(body: CatalogInstall) -> dict:
    return await dash_send("POST", "/api/mcp/catalog/install", body.model_dump())


@app.post("/mcp/servers/{name}/auth")
async def start_mcp_oauth(name: str) -> dict:
    """Kicks off OAuth for an already-installed server and returns the
    authorization_url to open plus a flow_id to poll -- mirrors the real
    desktop app's inline-card OAuth connect button."""
    return await dash_send("POST", f"/api/mcp/servers/{name}/auth", None)


@app.get("/mcp/oauth/flows/{flow_id}")
async def get_mcp_oauth_flow(flow_id: str) -> Any:
    return await dash_get(f"/api/mcp/oauth/flows/{flow_id}")


@app.delete("/mcp/oauth/flows/{flow_id}")
async def cancel_mcp_oauth_flow(flow_id: str) -> dict:
    return await dash_send("DELETE", f"/api/mcp/oauth/flows/{flow_id}", None)


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------

@app.get("/skills")
async def list_skills() -> Any:
    # See list_connectors' own comment on scrub_branding_deep -- the
    # bundled "hermes-agent"/"hermes-agent-skill-authoring" skill entries
    # carry their own names/descriptions verbatim otherwise.
    return persona.scrub_branding_deep(await dash_get("/api/skills"))


class SkillToggleBody(BaseModel):
    name: str
    enabled: bool


@app.put("/skills/toggle")
async def toggle_skill(body: SkillToggleBody) -> dict:
    return await dash_send("PUT", "/api/skills/toggle", body.model_dump())


# --------------------------------------------------------------------------
# Environment variables
# --------------------------------------------------------------------------

@app.get("/env")
async def list_env() -> Any:
    return await dash_get("/api/env")


class EnvSet(BaseModel):
    key: str
    value: str


@app.put("/env")
async def set_env(body: EnvSet) -> dict:
    return await dash_send("PUT", "/api/env", body.model_dump())


@app.delete("/env/{key}")
async def delete_env(key: str) -> dict:
    return await dash_send("DELETE", "/api/env", {"key": key})


# --------------------------------------------------------------------------
# Cron -- general view (all jobs, every profile), distinct from the
# bot-scoped /bots/{name}/routines above.
# --------------------------------------------------------------------------

@app.get("/cron")
async def list_all_cron_jobs() -> Any:
    jobs = await dash_get("/api/cron/jobs", profile="all")
    return jobs if isinstance(jobs, list) else jobs.get("data", [])


class CronCreate(BaseModel):
    name: str = ""
    prompt: str
    schedule: str
    deliver: str = "local"


@app.post("/cron")
async def create_cron_job(body: CronCreate) -> dict:
    return await dash_send("POST", "/api/cron/jobs", body.model_dump())


class CronUpdate(BaseModel):
    updates: dict


@app.put("/cron/{job_id}")
async def update_cron_job(job_id: str, body: CronUpdate) -> dict:
    return await dash_send("PUT", f"/api/cron/jobs/{job_id}", body.model_dump())


@app.delete("/cron/{job_id}")
async def delete_cron_job(job_id: str) -> dict:
    return await dash_send("DELETE", f"/api/cron/jobs/{job_id}", None)


@app.post("/cron/{job_id}/pause")
async def pause_cron_job(job_id: str) -> dict:
    return await dash_send("POST", f"/api/cron/jobs/{job_id}/pause", None)


@app.post("/cron/{job_id}/resume")
async def resume_cron_job(job_id: str) -> dict:
    return await dash_send("POST", f"/api/cron/jobs/{job_id}/resume", None)


@app.post("/cron/{job_id}/run")
async def run_cron_job(job_id: str) -> dict:
    return await dash_send("POST", f"/api/cron/jobs/{job_id}/trigger", None)


# --------------------------------------------------------------------------
# Plugins (read-only for now)
# --------------------------------------------------------------------------

@app.get("/plugins")
async def list_plugins() -> Any:
    # See list_connectors' own comment on scrub_branding_deep -- read-only
    # display (no PUT here), so scrubbing the "hermes-achievements" plugin
    # entry's own name/description can't break a round-trip toggle action.
    return persona.scrub_branding_deep(await dash_get("/api/dashboard/plugins"))


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------

@app.get("/webhooks")
async def list_webhooks() -> Any:
    return await dash_get("/api/webhooks")


@app.post("/webhooks/enable")
async def enable_webhooks() -> dict:
    return await dash_send("POST", "/api/webhooks/enable", None)


class WebhookCreateBody(BaseModel):
    name: str
    description: Optional[str] = None
    events: list[str] = []
    prompt: Optional[str] = None
    deliver: str = "log"


@app.post("/webhooks")
async def create_webhook(body: WebhookCreateBody) -> dict:
    return await dash_send("POST", "/api/webhooks", body.model_dump())


@app.delete("/webhooks/{name}")
async def delete_webhook(name: str) -> dict:
    return await dash_send("DELETE", f"/api/webhooks/{name}", None)


@app.put("/webhooks/{name}/enabled")
async def set_webhook_enabled(name: str, body: ToggleBody) -> dict:
    return await dash_send("PUT", f"/api/webhooks/{name}/enabled", body.model_dump())


# --------------------------------------------------------------------------
# Files (scoped to the default profile's workspace)
# --------------------------------------------------------------------------

@app.get("/files")
async def list_files(path: str = "") -> Any:
    return await dash_get("/api/files", query={"path": path} if path else None)


@app.get("/files/read")
async def read_file(path: str) -> Any:
    return await dash_get("/api/files/read", query={"path": path})


class MkdirBody(BaseModel):
    path: str


@app.post("/files/mkdir")
async def mkdir(body: MkdirBody) -> dict:
    return await dash_send("POST", "/api/files/mkdir", body.model_dump())


class FileDelete(BaseModel):
    path: str
    recursive: bool = False


@app.delete("/files")
async def delete_file(body: FileDelete) -> dict:
    return await dash_send("DELETE", "/api/files", body.model_dump())


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------

@app.get("/logs")
async def get_logs(lines: int = 200) -> Any:
    return await dash_get("/api/logs", lines=lines)


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------

@app.get("/system")
async def system_status() -> dict:
    status, stats = await asyncio.gather(dash_get("/api/status"), dash_get("/api/system/stats"))
    return {"status": status, "stats": stats}


@app.post("/system/restart-gateway")
async def restart_gateway() -> dict:
    return await dash_send("POST", "/api/gateway/restart", None)


# --------------------------------------------------------------------------
# Config -- raw YAML editor (the safe, always-correct fallback: every
# structured setting also lives in this same file, so a raw editor never
# lags behind whatever new keys a Hermes update introduces).
# --------------------------------------------------------------------------

@app.get("/config/raw")
async def get_config_raw() -> Any:
    return await dash_get("/api/config/raw")


class ConfigRawBody(BaseModel):
    yaml_text: str


@app.put("/config/raw")
async def put_config_raw(body: ConfigRawBody) -> dict:
    return await dash_send("PUT", "/api/config/raw", body.model_dump())


@app.get("/ready")
async def ready() -> dict:
    """Readiness probe: 200 once the embedded chat engine can actually run,
    and the external dashboard too when one is configured (profile/MCP/
    skills/env/cron admin CRUD still depends on it -- see engine.py's
    module docstring for why that part isn't embedded yet). A deployment
    with no dashboard configured is still real and ready for chat, so
    admin-surface reachability is only checked when it applies.
    """
    if not _engine._use_http():
        # Only meaningful for the embedded transport -- under http, chat
        # goes through per-bot dedicated workers (bot_processes.py), not
        # this shared in-process adapter, so constructing it here would
        # check something readiness no longer depends on.
        try:
            _engine._get_adapter()
        except Exception as exc:
            return JSONResponse(status_code=503, content={"ok": False, "detail": f"engine not ready: {exc}"})

    if os.environ.get("HERMES_DASHBOARD_URL"):
        try:
            r = await _dash_client.get("/api/status")
            if r.status_code >= 400:
                return JSONResponse(status_code=503, content={"ok": False, "detail": f"dashboard status HTTP {r.status_code}"})
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=503, content={"ok": False, "detail": f"dashboard unreachable: {exc}"})

    return {"ok": True}


@app.get("/version")
async def version() -> dict:
    return {"sha": os.environ.get("GIT_SHA", ""), "built": os.environ.get("BUILT_AT", "")}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------------
# Bot worker lifecycle -- keep-warm classification and idle reaping, only
# meaningful under ZBOTS_CHAT_TRANSPORT=http (see bot_processes.py's own
# module docstring for the architecture this replaces). A bot's lifecycle
# mode is derived, not a manual per-bot flag: any bot with at least one
# enabled routine is treated as keep-warm (its own cron scheduler only
# runs while ITS OWN worker process is alive -- there is no external "wake
# me before my cron fires" mechanism, so a routine-bearing bot has to stay
# up for that routine to ever fire at all), "default" is always keep-warm
# (it also carries messaging connectors), everything else is on-demand:
# spawned lazily by send_to_bot/stream_to_bot's own
# ensure_bot_process_running() call, reaped here after IDLE_REAP_SECONDS
# of inactivity.
# --------------------------------------------------------------------------

IDLE_REAP_SECONDS = float(os.environ.get("BOT_IDLE_REAP_SECONDS", str(30 * 60)))
IDLE_REAP_INTERVAL_SECONDS = float(os.environ.get("BOT_IDLE_REAP_INTERVAL_SECONDS", "300"))


# Categories that stay keep-warm unconditionally, regardless of whether
# they happen to have a routine: chore (its own scheduler-adjacent
# delivery reliability -- same reasoning that already made
# routine-presence a keep-warm signal, now explicit instead of inferred),
# developer (long-running autonomous work, needs to survive well past any
# idle-reap sweep), supervisor (runs its own periodic QC routine and
# needs to be responsive to check in on other bots on demand too).
_KEEP_WARM_CATEGORIES = frozenset({"chore", "developer", "supervisor"})


async def _keep_warm_bots(profile_names: list[str]) -> set[str]:
    """"default" unconditionally, every bot whose own category is
    chore/developer/supervisor (see _KEEP_WARM_CATEGORIES), plus (kept,
    not replaced -- a "general"-category bot that happens to have an
    enabled routine should still stay warm for it) every profile with at
    least one ENABLED routine. One /api/cron/jobs?profile=all fetch, not
    one call per bot -- list_routines()'s own per-bot endpoint exists for
    the UI, not for a loop over the whole roster.
    """
    state = _read_state()
    categories = state.get("categories") or {}
    keep_warm = {"default"} & set(profile_names)
    for name in profile_names:
        if categories.get(name, "general") in _KEEP_WARM_CATEGORIES:
            keep_warm.add(name)
    try:
        jobs = await dash_get("/api/cron/jobs", profile="all")
    except Exception:
        return keep_warm  # best-effort -- worst case, an on-demand bot with a routine wakes late once
    rows = jobs if isinstance(jobs, list) else jobs.get("data", [])
    for j in rows:
        if not j.get("enabled"):
            continue
        bot = _routine_bot_from_job(j)
        if bot in profile_names:
            keep_warm.add(bot)
    return keep_warm


async def _get_roster_with_retry(*, attempts: int = 5, delay_s: float = 2.0) -> list[RosterEntry]:
    """Real bug found live: the FastAPI backend's own startup event can
    fire before the dashboard (a separate process, port 9119, started in
    its own background subshell by entrypoint.sh with no explicit
    ordering/wait between the two) is actually accepting connections yet
    -- confirmed live, get_roster() at startup time failed with a
    connection error, the old single-attempt startup handler silently
    gave up on the whole keep-warm spawn, and "default" was never
    started at all under ZBOTS_CHAT_TRANSPORT=http (chat broken until the
    next manual redeploy). A short retry loop absorbs that ordinary
    startup race without needing to reorder entrypoint.sh's own process
    launches.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return await get_roster(include_hidden=True)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(delay_s)
    raise last_exc  # type: ignore[misc]


async def _ensure_keep_warm_bots_running(keep_warm: set[str]) -> None:
    results = await asyncio.gather(
        *(bot_processes.ensure_bot_process_running(name) for name in keep_warm),
        return_exceptions=True,
    )
    for _name, result in zip(keep_warm, results):
        if isinstance(result, Exception):
            # Best-effort: one keep-warm bot failing to boot (a bad
            # provider config, a genuinely unreachable model endpoint)
            # shouldn't take the whole container down -- the next
            # lifecycle sweep retries it, or a chat request wakes it
            # directly.
            pass


@app.on_event("startup")
async def _spawn_keep_warm_bots_and_start_reaper() -> None:
    if not _engine._use_http():
        return
    asyncio.create_task(_bot_lifecycle_sweep_loop())
    try:
        roster = await _get_roster_with_retry()
    except Exception:
        # Dashboard still unreachable after every retry -- the periodic
        # sweep below (same self-healing logic, not just a startup-only
        # attempt) will keep trying every IDLE_REAP_INTERVAL_SECONDS.
        return
    keep_warm = await _keep_warm_bots([e.name for e in roster])
    await _ensure_keep_warm_bots_running(keep_warm)


async def _bot_lifecycle_sweep_loop() -> None:
    """Periodic self-healing sweep: re-ensures every keep-warm bot is
    actually running (not just a startup-time attempt -- a keep-warm bot
    that failed to boot, or was manually stopped, stays down forever
    otherwise) AND reaps idle on-demand bots. Same roster read powers
    both halves of one sweep.
    """
    while True:
        await asyncio.sleep(IDLE_REAP_INTERVAL_SECONDS)
        try:
            roster = await get_roster(include_hidden=True)
            keep_warm = await _keep_warm_bots([e.name for e in roster])
            await _ensure_keep_warm_bots_running(keep_warm)
            idle_since = {e.name: e.last_active for e in roster if e.last_active is not None}
            await bot_processes.reap_idle(idle_since=idle_since, keep_warm=keep_warm, threshold_s=IDLE_REAP_SECONDS)
        except Exception:
            # Best-effort: a failed sweep just means bots stay warm (or
            # asleep) a little longer than ideal -- never worth crashing
            # the loop over, the next interval tries again.
            pass


@app.on_event("shutdown")
async def _stop_all_bot_processes() -> None:
    """Best-effort clean shutdown of every worker this backend process
    spawned -- container stop/restart otherwise leaves them running as
    orphans (still holding their ports, still burning memory) until
    something notices via a liveness check on next boot. Only meaningful
    ones this process actually tracks in memory (bot_processes._processes)
    get a real SIGTERM here; a worker recorded in the on-disk registry by
    a PRIOR backend process (this process never held its handle) is left
    alone -- exactly the case _is_worker_alive's own pid check already
    handles correctly on the next boot.
    """
    if not _engine._use_http():
        return
    await asyncio.gather(
        *(bot_processes.stop_bot_process(name) for name in list(bot_processes._processes.keys())),
        return_exceptions=True,
    )
