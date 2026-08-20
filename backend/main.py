"""Bots UI backend -- a small companion API that gives the web dashboard the
multi-bot management surface Hermes' desktop app has natively (Bot Mode) and
the web dashboard doesn't ship.

Talks to two ALREADY-REAL Hermes surfaces, both loopback-only from inside
this same container:

  - the dashboard's own REST API on 127.0.0.1:9119 (HTTP basic auth) for
    profile CRUD, config, and cron/routines -- exactly what the bundled
    ProfilesPage/CronPage already call.
  - the api_server platform on 127.0.0.1:8642 (Bearer API_SERVER_KEY) for
    actually chatting with a bot: this is the same session-based chat
    protocol `hermes peer dm` uses (find-or-create a session titled
    "Bot Chat" for that profile via the /p/<profile>/ multiplex mirror,
    POST a message, read the reply).

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
from pydantic import BaseModel

DASHBOARD_BASE = "http://127.0.0.1:9119"
API_SERVER_BASE = "http://127.0.0.1:8642"

DASHBOARD_USER = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "")
DASHBOARD_PASS = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "")
API_SERVER_KEY = os.environ.get("API_SERVER_KEY", "")

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

_state_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# Local state (hidden bots / avatar choices / group definitions)
# --------------------------------------------------------------------------

def _default_state() -> dict[str, Any]:
    return {"hidden": [], "avatars": {}, "groups": {}, "titles": {}, "active_sessions": {}}


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    data.setdefault("hidden", [])
    data.setdefault("avatars", {})
    data.setdefault("groups", {})
    data.setdefault("titles", {})
    data.setdefault("active_sessions", {})
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
        raise HTTPException(status_code=502, detail=f"Could not authenticate to the Hermes dashboard: HTTP {r.status_code}")
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


async def dash_get(path: str, **params) -> Any:
    r = await _dashboard_request("GET", path, params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    return r.json()


async def dash_send(method: str, path: str, body: Optional[dict] = None) -> Any:
    r = await _dashboard_request(method, path, json_body=body)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    if not r.content:
        return {}
    return r.json()


# --------------------------------------------------------------------------
# api_server client (Bearer auth, port 8642) -- actual bot chat
# --------------------------------------------------------------------------

def _bot_base(profile: str) -> str:
    # Every bot is a profile under the SAME running gateway; the multiplex
    # mirror (/p/<profile>/) selects which profile's model/skills config
    # handles the request. IMPORTANT constraint found live: it does NOT
    # give each profile an isolated session store -- sessions created via
    # any /p/<profile>/ prefix land in the one shared state.db the main
    # gateway process already has open (confirmed: profile_name/session_key
    # /chat_id/origin_json are all NULL on a multiplexed session row, and no
    # per-profile state.db file is ever created). So instead of Hermes' own
    # peer.py convention (a single globally-titled "Bot Chat" session, which
    # only stays unique across genuinely separate gateway processes/peers),
    # each bot here gets its own uniquely-titled session -- see
    # _bot_chat_title(). That keeps every bot's thread correctly isolated
    # even though they're all rows in the same physical table.
    if profile == "default":
        return API_SERVER_BASE
    return f"{API_SERVER_BASE}/p/{profile}"


_BOT_SESSION_PREFIX = "[Bots UI]"


def _bot_chat_title(profile: str, n: int = 1) -> str:
    base = f"{_BOT_SESSION_PREFIX} {profile}"
    return base if n <= 1 else f"{base} #{n}"


_BOT_SESSION_SUFFIX_RE = re.compile(r"^\[Bots UI\] ([a-zA-Z0-9_-]+)(?: #(\d+))?$")


def _bot_session_rollover_n(title: str, profile: str) -> Optional[int]:
    """If title belongs to this bot's session family, its rollover number (1 if bare)."""
    m = _BOT_SESSION_SUFFIX_RE.match((title or "").strip())
    if not m or m.group(1) != profile:
        return None
    return int(m.group(2)) if m.group(2) else 1


def _api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_SERVER_KEY}", "Content-Type": "application/json"}


async def _list_bot_sessions(client: httpx.AsyncClient, base: str, profile: str) -> list[dict]:
    """Every session that has ever belonged to this bot (all rollovers), oldest first.

    Sessions created through the /p/<profile>/ multiplex mirror share one
    physical session store with no persisted profile identity (see
    _bot_base's docstring) -- title is the only thing that reliably scopes
    a session to a bot, so this is a title-prefix scan, not a real query.
    """
    r = await client.get(f"{base}/api/sessions", params={"limit": 200}, headers=_api_headers())
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    rows = []
    for row in r.json().get("data") or []:
        if isinstance(row, dict) and _bot_session_rollover_n(row.get("title"), profile) is not None:
            rows.append(row)
    rows.sort(key=lambda row: row.get("started_at") or 0)
    return rows


async def _ensure_bot_chat_session(client: httpx.AsyncClient, base: str, profile: str) -> tuple[str, list[dict]]:
    """Return (this bot's current/active session id, all of its sessions).

    Prefers the session id already recorded as active in local state (set
    the last time a rollover happened) over a fresh title search, so a
    rollover doesn't get silently "found" and reused again next call --
    only used to bootstrap the very first session, or to recover if state
    was lost.
    """
    state = _read_state()
    active_id = (state.get("active_sessions") or {}).get(profile)
    all_sessions = await _list_bot_sessions(client, base, profile)
    if active_id and any(s.get("id") == active_id for s in all_sessions):
        return active_id, all_sessions
    if all_sessions:
        session_id = str(all_sessions[-1]["id"])
        await _mutate_state(lambda d: d.setdefault("active_sessions", {}).__setitem__(profile, session_id))
        return session_id, all_sessions

    r = await client.post(
        f"{base}/api/sessions",
        json={"title": _bot_chat_title(profile), "source": "bots_ui"},
        headers=_api_headers(),
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    payload = r.json()
    session = payload.get("session") if isinstance(payload.get("session"), dict) else payload
    session_id = str(session.get("id") or session.get("session_id") or "")
    if not session_id:
        raise HTTPException(status_code=502, detail="api_server did not return a session id")
    await _mutate_state(lambda d: d.setdefault("active_sessions", {}).__setitem__(profile, session_id))
    return session_id, [session]


async def _roll_over_bot_session(client: httpx.AsyncClient, base: str, profile: str, all_sessions: list[dict]) -> str:
    """Start a fresh session for this bot after its current one wedges.

    The old session is NOT deleted -- its history stays real and readable
    (get_bot_messages merges every rollover's messages back together), so
    a server-side bug that wedges a session no longer means losing the
    conversation, just starting a new physical session under the hood.
    """
    next_n = 1 + max((_bot_session_rollover_n(s.get("title"), profile) or 0) for s in all_sessions)
    r = await client.post(
        f"{base}/api/sessions",
        json={"title": _bot_chat_title(profile, next_n), "source": "bots_ui"},
        headers=_api_headers(),
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    payload = r.json()
    session = payload.get("session") if isinstance(payload.get("session"), dict) else payload
    session_id = str(session.get("id") or session.get("session_id") or "")
    if not session_id:
        raise HTTPException(status_code=502, detail="api_server did not return a session id")
    await _mutate_state(lambda d: d.setdefault("active_sessions", {}).__setitem__(profile, session_id))
    return session_id


async def send_to_bot(profile: str, message: str, *, timeout: float = 300.0) -> str:
    """Send one message to a bot's active session; return the reply text.

    Real, recurring bug found live (not a one-off): a session that answers
    correctly on its first turn can start 500ing on every turn after --
    confirmed root cause via the server's own traceback (agent.log), even
    though the api_server's error response itself is just a generic "Server
    got itself in trouble" plain-text page with no detail to match on.
    Real root cause (traced through agent_init.py/api_server.py source,
    live-tested against half a dozen workarounds -- explicit provider/model
    in the body, a Hermes-native provider entry, the virtual "hermes-agent"
    model alias, session forking -- none of which avoided it): once a
    session's `model` field gets persisted as a real provider model string
    (which happens automatically after its first turn), the NEXT turn's
    resolver re-reads that stored string and tries to resolve it as a route
    alias instead of a raw model id, fails, and falls through to an
    unconfigured "custom/main" placeholder. This is inside Hermes' own
    closed agent_init.py/api_server.py, not fixable from here, and appears
    to affect every session after its first turn, multiplexed or not.

    Self-healing instead: on any 500, roll over to a brand-new session
    (kept, not deleted, so its own history is still real) and retry once.
    get_bot_messages() merges every rollover back into one continuous
    thread, so this is invisible to the user beyond the reply arriving
    from a "new" session under the hood.
    """
    base = _bot_base(profile)
    async with httpx.AsyncClient(timeout=timeout) as client:
        session_id, all_sessions = await _ensure_bot_chat_session(client, base, profile)
        r = await client.post(
            f"{base}/api/sessions/{session_id}/chat",
            json={"message": message},
            headers=_api_headers(),
        )
        if r.status_code == 500:
            session_id = await _roll_over_bot_session(client, base, profile, all_sessions)
            r = await client.post(
                f"{base}/api/sessions/{session_id}/chat",
                json={"message": message},
                headers=_api_headers(),
            )
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code, detail=r.text[:500])
        result = r.json()
        msg = result.get("message")
        return str(msg.get("content") or "") if isinstance(msg, dict) else ""


async def get_bot_activity(profile: str) -> dict:
    """This bot's own most-recent session summary for the roster (preview/
    timestamp/active) -- across its whole session family (see
    _list_bot_sessions), NOT "most recent session on this multiplex base",
    since that base is shared across every bot on this gateway (see
    _bot_base's docstring) and would otherwise surface a completely
    unrelated bot's/human's activity.
    """
    base = _bot_base(profile)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            sessions = await _list_bot_sessions(client, base, profile)
    except HTTPException:
        return {}
    if not sessions:
        return {}
    session = sessions[-1]
    last_active = session.get("last_active") or session.get("started_at")
    is_active = session.get("ended_at") is None and last_active is not None and (time.time() - last_active) < 300
    return {
        "preview": session.get("preview") or "",
        "last_active": last_active,
        "is_active": is_active,
    }


async def get_bot_messages(profile: str, limit: int = 200) -> list[dict]:
    """Every message across this bot's whole session family, oldest first.

    Not just its current active session: a server-side bug (see
    send_to_bot's docstring) can force a mid-conversation rollover to a new
    physical session, and the point of tracking the whole family is exactly
    so that rollover doesn't make earlier messages disappear from the
    user's view.
    """
    base = _bot_base(profile)
    async with httpx.AsyncClient(timeout=30) as client:
        sessions = await _list_bot_sessions(client, base, profile)
        if not sessions:
            return []
        all_messages: list[dict] = []
        for session in sessions:
            r = await client.get(
                f"{base}/api/sessions/{session['id']}/messages",
                params={"limit": limit},
                headers=_api_headers(),
            )
            if r.status_code >= 400:
                continue
            payload = r.json()
            all_messages.extend(payload.get("data") or payload.get("messages") or [])
        all_messages.sort(key=lambda m: m.get("timestamp") or 0)
        return all_messages[-limit:]


# --------------------------------------------------------------------------
# Roster
# --------------------------------------------------------------------------

def _validate_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Bot name must be lowercase alphanumeric (with - or _), max 64 chars.")
    return name


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


@app.get("/roster")
async def get_roster(include_hidden: bool = False) -> list[RosterEntry]:
    profiles_resp = await dash_get("/api/profiles")
    profiles = profiles_resp.get("profiles", []) if isinstance(profiles_resp, dict) else profiles_resp
    state = _read_state()
    hidden = set(state.get("hidden") or [])
    avatars = state.get("avatars") or {}
    titles = state.get("titles") or {}

    visible = [p for p in profiles if p.get("name") and (include_hidden or p["name"] not in hidden)]
    activity = await asyncio.gather(*(get_bot_activity(p["name"]) for p in visible))

    entries: list[RosterEntry] = []
    for p, latest in zip(visible, activity):
        name = p["name"]
        entries.append(
            RosterEntry(
                name=name,
                title=p.get("display_name") or titles.get(name) or _pretty_title(name),
                description=p.get("description") or "",
                model=p.get("model"),
                provider=p.get("provider"),
                gateway_running=bool(p.get("gateway_running")),
                is_active=bool(latest.get("is_active")),
                is_hidden=name in hidden,
                preview=latest.get("preview") or "",
                last_active=latest.get("last_active"),
                avatar=avatars.get(name) or {"type": "blob"},
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
    if body.title:
        await _mutate_state(lambda d: d["titles"].__setitem__(name, body.title))
    if body.soul:
        await dash_send("PUT", f"/api/profiles/{name}/soul", {"content": body.soul})
    roster = await get_roster(include_hidden=True)
    for entry in roster:
        if entry.name == name:
            return entry
    raise HTTPException(status_code=500, detail="Bot created but not found in roster afterward.")


class BotUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    soul: Optional[str] = None


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
    if body.description is not None:
        await dash_send("PUT", f"/api/profiles/{name}/description", {"description": body.description})
    if body.provider and body.model:
        await dash_send("PUT", f"/api/profiles/{name}/model", {"provider": body.provider, "model": body.model})
    if body.soul is not None:
        await dash_send("PUT", f"/api/profiles/{name}/soul", {"content": body.soul})
    return {"ok": True}


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


@app.get("/bots/{name}/messages")
async def bot_messages(name: str) -> list[dict]:
    name = _validate_name(name)
    return await get_bot_messages(name)


@app.post("/bots/{name}/messages")
async def bot_send(name: str, body: SendMessage) -> dict:
    name = _validate_name(name)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text required.")
    reply = await send_to_bot(name, text)
    return {"reply": reply}


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
    for round_num in range(GROUP_MAX_ROUNDS):
        if not round_targets or sent >= GROUP_MAX_MESSAGES:
            break
        next_targets: set[str] = set()
        for target in round_targets:
            if sent >= GROUP_MAX_MESSAGES:
                break
            context = body.text if round_num == 0 else f"(replying in group '{group['name']}') {body.text}"
            try:
                reply = await send_to_bot(target, context)
            except HTTPException as exc:
                reply = f"[error contacting {target}: {exc.detail}]"
            entry = {"from": target, "text": reply, "ts": time.time(), "round": round_num + 1}
            await _append_group_message(group_id, entry)
            posted.append(entry)
            sent += 1
            for mention in _MENTION_RE.findall(reply):
                if mention in members and mention != target:
                    next_targets.add(mention)
        round_targets = list(next_targets)

    return {"ok": True, "messages": posted}


# --------------------------------------------------------------------------
# Routines (real hermes cron jobs, namespaced "[bot:<name>] <routine>")
# --------------------------------------------------------------------------

def _routine_job_name(bot: str, routine: str) -> str:
    return f"[bot:{bot}] {routine}"


def _routine_bot_from_job(job: dict) -> Optional[str]:
    name = job.get("name") or ""
    m = re.match(r"^\[bot:([a-zA-Z0-9_-]+)\]", name)
    return m.group(1) if m else None


@app.get("/bots/{name}/routines")
async def list_routines(name: str) -> list[dict]:
    name = _validate_name(name)
    jobs = await dash_get("/api/cron/jobs", profile="all")
    rows = jobs if isinstance(jobs, list) else jobs.get("data", [])
    return [j for j in rows if _routine_bot_from_job(j) == name]


class RoutineCreate(BaseModel):
    routine: str  # short label, becomes part of the job name
    prompt: str
    schedule: str  # raw hermes cron schedule string


@app.post("/bots/{name}/routines")
async def create_routine(name: str, body: RoutineCreate) -> dict:
    name = _validate_name(name)
    return await dash_send(
        "POST",
        f"/api/cron/jobs?profile={name}",
        {
            "name": _routine_job_name(name, body.routine),
            "prompt": body.prompt,
            "schedule": body.schedule,
            "deliver": "local",
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
    return await dash_send("POST", "/api/providers/custom-endpoints", body.model_dump())


@app.post("/providers/{provider_id}/activate")
async def activate_provider(provider_id: str) -> dict:
    return await dash_send("POST", f"/api/providers/custom-endpoints/{provider_id}/activate", None)


class ModelActivate(BaseModel):
    provider: str
    model: str


@app.post("/models/activate")
async def activate_model(body: ModelActivate) -> dict:
    """Set one specific model (not necessarily a provider's own default) as
    the active main-slot model -- distinct from /providers/{id}/activate,
    which always uses whatever model the provider entry itself points at.
    """
    return await dash_send("POST", "/api/model/set", {"scope": "main", "provider": body.provider, "model": body.model, "task": ""})


@app.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    return await dash_send("DELETE", f"/api/providers/custom-endpoints/{provider_id}", None)


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
    Session deletion only exists on the api_server (port 8642, Bearer
    auth), the same one bot chat already uses.
    """
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
# Skills
# --------------------------------------------------------------------------

@app.get("/skills")
async def list_skills() -> Any:
    return await dash_get("/api/skills")


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
    return await dash_get("/api/dashboard/plugins")


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
    return await dash_get("/api/files", **({"path": path} if path else {}))


@app.get("/files/read")
async def read_file(path: str) -> Any:
    return await dash_get("/api/files/read", path=path)


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


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
