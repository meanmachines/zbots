"""Embedded engine bridge -- runs chat/session logic in-process instead of
over loopback HTTP to a separately-running gateway.

Why this exists and what it deliberately does NOT do: the engine's own
chat/session handlers (gateway/platforms/api_server.py, APIServerAdapter)
are written as aiohttp route handlers -- they take a real web.Request and
return a real web.Response, and some of them (session creation in
particular) run an atomic check-insert transaction inline in the handler
rather than through a simple reusable method. Reimplementing that by hand
risks quietly breaking the TOCTOU-safety it was built for. So instead of
extracting and reimplementing that logic, this module constructs the
adapter for real (same construction sequence gateway/run.py uses, minus
the parts that open a network listener) and calls its actual handler
methods directly, using aiohttp's own make_mocked_request to build a
request object without a real HTTP connection. This reuses the real,
tested logic and stays correct automatically when the vendored snapshot is
updated -- see vendor/VENDORED_COMMIT.md.

What zBots gets from this: one process instead of two, no loopback network
hop for the actual chat path, and the same profile-scoping/session-safety
guarantees the real gateway has, because this IS the real gateway code,
just not listening on a socket.

Profile/config CRUD (profiles, MCP servers, skills, env vars, cron) is
deliberately NOT embedded -- see the project plan for why: the real logic
for those lives inside hermes_cli/web_server.py, a ~19000-line module with
real import-time side effects (a second FastAPI app, singletons). Those
stay on the existing HTTP calls in main.py.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    from . import resilience
except ImportError:
    import resilience

VENDOR_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "hermes-agent"
if str(VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VENDOR_ROOT))

_runner = None
_adapter = None


def _build_runner_and_adapter():
    """One-time construction, mirroring gateway/run.py's start_gateway()
    exactly for the pieces we need -- GatewayRunner() then
    APIServerAdapter() wired to it -- and deliberately skipping
    runner.start() (spawns every configured platform, PID file) and
    adapter.connect() (binds a real network listener). Neither is needed:
    we call the adapter's handler methods as plain Python calls, never
    over a socket. MCP tool discovery is also normally part of
    runner.start()'s sequence -- that piece IS needed (a configured
    mcp_servers entry is otherwise never connected, so no bot ever sees
    those tools), so it's triggered separately; see
    _ensure_mcp_tools_discovered below.
    """
    from gateway.run import load_gateway_config_for_runner, GatewayRunner
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.config import Platform

    config = load_gateway_config_for_runner()
    runner = GatewayRunner(config)
    platform_config = config.platforms.get(Platform.API_SERVER)
    if platform_config is None:
        raise RuntimeError(
            "No api_server platform config found for the active Hermes "
            "profile -- API_SERVER_KEY must be set for this profile."
        )
    adapter = APIServerAdapter(platform_config)
    adapter.gateway_runner = runner
    adapter.set_session_store(runner.session_store)
    return runner, adapter


def _get_adapter():
    global _runner, _adapter
    if _adapter is None:
        _runner, _adapter = _build_runner_and_adapter()
    return _adapter


def invalidate_adapter() -> None:
    """Force the next _get_adapter() call to rebuild from scratch.

    Real bug found live: a profile mutation made through the dashboard
    HTTP API (soul, model, ...) writes to disk correctly, but the
    embedded engine kept answering chat with the pre-mutation persona
    until the whole backend process was restarted -- confirmed by a
    controlled test (fresh session either way; only a process restart
    changed the outcome). config.yaml's own load_config() is smart about
    this (cached on the file's mtime/size, auto-invalidates), but
    whatever holds a profile's resolved persona inside the constructed
    GatewayRunner/APIServerAdapter apparently isn't. Rather than fully
    tracing that cache through hermes-agent's own source, this resets
    the one piece of caching zBots itself controls -- call it after any
    dash_send() that changes a profile's soul/model/skills, and the next
    chat call gets a fresh adapter instead of a stale one.
    """
    global _runner, _adapter
    _runner = None
    _adapter = None


_mcp_discovery_done = False
_mcp_discovery_lock = asyncio.Lock()


async def _ensure_mcp_tools_discovered() -> None:
    """One-time, lazy MCP tool discovery for the embedded engine.

    gateway/run.py's normal startup calls tools.mcp_tool.discover_mcp_tools()
    right before runner.start() -- both are part of the same sequence, but
    only runner.start() gets deliberately skipped here (see
    _build_runner_and_adapter's docstring for why). Skipping discovery too
    was never intentional, just a side effect of skipping start() wholesale
    -- confirmed live: a bot correctly listed every one of its other tools
    but had never heard of bot-supervisor's list_bots, despite it being
    registered in config.yaml's mcp_servers and the server itself running
    and reachable.

    discover_mcp_tools() is a standalone, idempotent, cross-process-locked
    function -- calling it here doesn't reimplement any of that, just
    triggers the one piece of runner.start() this embedding actually
    needs. Runs in the executor because it can block up to ~120s
    internally (a slow/unreachable MCP server), same as upstream's own
    call site; the lock only prevents two concurrent chat requests both
    kicking off a redundant discovery pass while the first is in flight.
    """
    global _mcp_discovery_done
    if _mcp_discovery_done:
        return
    async with _mcp_discovery_lock:
        if _mcp_discovery_done:
            return
        try:
            from tools.mcp_tool import discover_mcp_tools

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, discover_mcp_tools)
        except Exception:
            # Fail-soft, matching discover_mcp_tools' own contract ("safe
            # to call even when the mcp package is not installed") -- a
            # broken/unreachable MCP server should degrade the bot to
            # having one fewer tool, not break chat entirely.
            pass
        _mcp_discovery_done = True


def _profile_scope(profile: str):
    """Same context manager the real gateway uses to scope config/secrets
    to one profile for the duration of a call -- see api_server.py's
    APIServerAdapter._profile_scope. Required before any handler call:
    _check_auth and the agent-creation path both read the active profile
    off a contextvar this sets.
    """
    adapter = _get_adapter()
    return adapter._profile_scope(profile)


async def _call_handler(
    handler_name: str,
    *,
    method: str = "GET",
    path: str = "/",
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
    query: Optional[dict] = None,
    match_info: Optional[dict] = None,
) -> tuple[int, Any]:
    """Call one of the adapter's real aiohttp handler methods directly,
    building a request with aiohttp's own make_mocked_request instead of
    going over a socket. Returns (status_code, parsed_json_body_or_None) to
    match the shape callers already expect from an httpx response.
    """
    from aiohttp.test_utils import make_mocked_request
    from multidict import CIMultiDict

    adapter = _get_adapter()
    await _ensure_mcp_tools_discovered()
    handler = getattr(adapter, handler_name)

    request_headers = CIMultiDict(headers or {})
    payload = None
    if json_body is not None:
        import json as _json

        body_bytes = _json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(body_bytes))
        from aiohttp.streams import StreamReader
        from aiohttp.base_protocol import BaseProtocol

        payload = StreamReader(BaseProtocol(loop=None), limit=2**20, loop=None)
        payload.feed_data(body_bytes)
        payload.feed_eof()

    query_string = ""
    if query:
        from urllib.parse import urlencode

        query_string = urlencode({k: v for k, v in query.items() if v is not None})

    request = make_mocked_request(
        method,
        f"{path}?{query_string}" if query_string else path,
        headers=request_headers,
        payload=payload,
        match_info=match_info or {},
    )
    try:
        response = await handler(request)
    except Exception as exc:
        # A real aiohttp server catches exceptions raised inside a route
        # handler and turns them into a 500 response before a client ever
        # sees a raw exception -- calling the handler directly like this
        # skips that middleware, so an internal failure (e.g. the known
        # session-model-resolution bug send_to_bot's rollover logic exists
        # to catch) would otherwise propagate as an unhandled Python
        # exception instead of the 500 status callers check for.
        return 500, {"error": str(exc)}
    status = response.status
    body = None
    if response.body:
        import json as _json

        try:
            body = _json.loads(response.body)
        except (ValueError, TypeError):
            body = None
    return status, body


def _api_headers(api_server_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_server_key}"}


# --------------------------------------------------------------------------
# Bot chat -- same resilience logic as the old HTTP path, same reasons,
# just calling handlers in-process instead of over loopback HTTP. The two
# bugs the retry/rollover logic works around both live inside the engine's
# own agent_init.py/api_server.py, so they still happen here -- calling the
# same code in-process instead of over HTTP does not fix them. What counts
# as a failure worth retrying, and how, lives in resilience.py -- this
# module only owns the session bookkeeping (creating/finding/rolling over
# sessions) that those decisions get applied against.
# --------------------------------------------------------------------------

_BOT_SESSION_PREFIX = "[Bots UI]"
_BOT_SESSION_SUFFIX_RE = re.compile(r"^\[Bots UI\] ([a-zA-Z0-9_-]+)(?: #(\d+))?$")


def _bot_chat_title(profile: str, n: int = 1) -> str:
    base = f"{_BOT_SESSION_PREFIX} {profile}"
    return base if n <= 1 else f"{base} #{n}"


def _bot_session_rollover_n(title: str, profile: str) -> Optional[int]:
    """If title belongs to this bot's session family, its rollover number (1 if bare)."""
    m = _BOT_SESSION_SUFFIX_RE.match((title or "").strip())
    if not m or m.group(1) != profile:
        return None
    return int(m.group(2)) if m.group(2) else 1


async def _list_bot_sessions(profile: str, headers: dict) -> list[dict]:
    """Every session that has ever belonged to this bot (all rollovers), oldest first."""
    with _profile_scope(profile):
        status, body = await _call_handler(
            "_handle_list_sessions",
            method="GET",
            path="/api/sessions",
            query={"limit": 200},
            headers=headers,
        )
    if status >= 400:
        raise RuntimeError(f"list_sessions failed: {status} {body}")
    rows = []
    for row in (body or {}).get("data") or []:
        if isinstance(row, dict) and _bot_session_rollover_n(row.get("title"), profile) is not None:
            rows.append(row)
    rows.sort(key=lambda row: row.get("started_at") or 0)
    return rows


async def _create_bot_session(profile: str, title: str, headers: dict) -> str:
    with _profile_scope(profile):
        status, body = await _call_handler(
            "_handle_create_session",
            method="POST",
            path="/api/sessions",
            json_body={"title": title, "source": "bots_ui"},
            headers=headers,
        )
    if status >= 400:
        raise RuntimeError(f"create_session failed: {status} {body}")
    session = (body or {}).get("session") if isinstance((body or {}).get("session"), dict) else body
    session_id = str(session.get("id") or "")
    if not session_id:
        raise RuntimeError("engine did not return a session id")
    return session_id


async def _ensure_bot_chat_session(
    profile: str, headers: dict, active_session_id: Optional[str]
) -> tuple[str, list[dict]]:
    """Return (this bot's current/active session id, all of its sessions).

    Prefers the caller-supplied active session id (the last one recorded
    after a rollover) over a fresh title search, so a rollover doesn't get
    silently "found" and reused again next call -- only used to bootstrap
    the very first session, or to recover if state was lost.
    """
    all_sessions = await _list_bot_sessions(profile, headers)
    if active_session_id and any(s.get("id") == active_session_id for s in all_sessions):
        return active_session_id, all_sessions
    if all_sessions:
        return str(all_sessions[-1]["id"]), all_sessions
    session_id = await _create_bot_session(profile, _bot_chat_title(profile), headers)
    return session_id, [{"id": session_id}]


async def _roll_over_bot_session(profile: str, headers: dict, all_sessions: list[dict]) -> str:
    """Start a fresh session for this bot after its current one wedges.

    The old session is NOT deleted -- its history stays real and readable
    (get_bot_messages merges every rollover's messages back together), so a
    server-side bug that wedges a session no longer means losing the
    conversation, just starting a new physical session under the hood.
    """
    next_n = 1 + max((_bot_session_rollover_n(s.get("title"), profile) or 0) for s in all_sessions)
    return await _create_bot_session(profile, _bot_chat_title(profile, next_n), headers)


async def send_to_bot(
    profile: str, message: str, api_server_key: str, active_session_id: Optional[str]
) -> tuple[str, str]:
    """Send one message to a bot's active session; return (reply_text, session_id).

    The failure classes this recovers from -- and exactly what recovering
    means for each -- are documented in resilience.py, not here. This
    function only runs the attempt sequence: chat, ask resilience.evaluate
    what the outcome means, act on it, and (for a rollover) hand the check
    a second chance to fire again on the retried attempt. A rollover
    session is kept, not deleted -- get_bot_messages merges every
    rollover back into one continuous thread, so retrying under the hood
    is invisible to the user beyond the reply arriving from a "new"
    session.
    """
    headers = _api_headers(api_server_key)
    session_id, all_sessions = await _ensure_bot_chat_session(profile, headers, active_session_id)

    async def _chat(sid: str) -> tuple[int, Optional[dict]]:
        with _profile_scope(profile):
            return await _call_handler(
                "_handle_session_chat",
                method="POST",
                path=f"/api/sessions/{sid}/chat",
                json_body={"message": message},
                headers=headers,
                match_info={"session_id": sid},
            )

    def _reply_of(body: Optional[dict]) -> str:
        msg = (body or {}).get("message")
        return str(msg.get("content") or "") if isinstance(msg, dict) else ""

    async def _rollover_and_retry(sid: str) -> tuple[str, int, Optional[dict]]:
        sid = await _roll_over_bot_session(profile, headers, all_sessions)
        status, body = await _chat(sid)
        return sid, status, body

    status, body = await _chat(session_id)
    decision = resilience.evaluate(status=status, body=body, reply="")
    if decision.mode is resilience.RetryMode.ROLLOVER:
        session_id, status, body = await _rollover_and_retry(session_id)
    if status >= 400:
        raise RuntimeError(f"chat failed: {status} {body}")
    reply = _reply_of(body)

    decision = resilience.evaluate(status=status, body=body, reply=reply)
    if decision.mode is resilience.RetryMode.SAME_SESSION:
        status, body = await _chat(session_id)
        follow_up = resilience.evaluate(status=status, body=body, reply="")
        if follow_up.mode is resilience.RetryMode.ROLLOVER:
            session_id, status, body = await _rollover_and_retry(session_id)
        if status >= 400:
            raise RuntimeError(f"chat retry failed: {status} {body}")
        retried = _reply_of(body)
        retried_decision = resilience.evaluate(status=status, body=body, reply=retried)
        if retried_decision.mode is not resilience.RetryMode.SAME_SESSION:
            reply = retried
        # Both attempts corrupted: return the first one rather than loop
        # forever -- the user still sees something.

    return reply, session_id


async def get_bot_messages(profile: str, api_server_key: str, limit: int = 200) -> list[dict]:
    """Every message across this bot's whole session family, oldest first.

    Not just its current active session: the rollover bug send_to_bot works
    around can force a mid-conversation move to a new physical session, and
    the point of tracking the whole family is exactly so that a rollover
    doesn't make earlier messages disappear from the user's view.
    """
    headers = _api_headers(api_server_key)
    sessions = await _list_bot_sessions(profile, headers)
    if not sessions:
        return []
    all_messages: list[dict] = []
    for session in sessions:
        with _profile_scope(profile):
            status, body = await _call_handler(
                "_handle_session_messages",
                method="GET",
                path=f"/api/sessions/{session['id']}/messages",
                query={"limit": limit},
                headers=headers,
                match_info={"session_id": session["id"]},
            )
        if status >= 400:
            continue
        payload = body or {}
        all_messages.extend(payload.get("data") or payload.get("messages") or [])
    all_messages.sort(key=lambda m: m.get("timestamp") or 0)
    return all_messages[-limit:]
