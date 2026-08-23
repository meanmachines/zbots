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

try:
    from . import persona
except ImportError:
    import persona

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
    if decision.mode is resilience.RetryMode.ROLLOVER:
        # Real bug found live: an existing session's model gets persisted
        # (locked) after its first turn; switching the *global* active
        # provider (Models page) doesn't touch that lock, so the next
        # message on this session sends the OLD provider's stale model id
        # to the NEW provider. A reachable-but-confused provider doesn't
        # fail the HTTP call over this -- it answers 200 with its own
        # rejection delivered as the reply text ("HTTP 400: <old-model> is
        # not a valid model ID"), which is exactly what
        # stale_model_lock_rolls_over is watching for. One retry, same as
        # every other rollover here.
        session_id, status, body = await _rollover_and_retry(session_id)
        if status >= 400:
            raise RuntimeError(f"chat retry failed: {status} {body}")
        reply = _reply_of(body)
    elif decision.mode is resilience.RetryMode.SAME_SESSION:
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

    # Applied last, after resilience checks (which look for the raw
    # <unused...> corruption pattern -- redacting first could interfere
    # with that match). See persona.redact_branding_leaks' own docstring
    # for why a deterministic scrub exists alongside the system-prompt
    # instruction rather than relying on the instruction alone.
    return persona.redact_branding_leaks(reply), session_id


# --------------------------------------------------------------------------
# Real streaming -- runs the engine's actual SSE handler in-process, same
# philosophy as _call_handler above (call the real, tested aiohttp handler
# method directly instead of reimplementing it) but that helper only
# supports a single final web.Response; _handle_session_chat_stream returns
# a web.StreamResponse, which prepares itself against the REQUEST's own
# writer rather than a real socket (aiohttp internal:
# StreamResponse._start() sets self._payload_writer = request._payload_writer,
# confirmed by reading aiohttp's own web_response.py). Supplying a custom
# writer whose write()/write_eof() push chunks onto an asyncio.Queue instead
# of a transport is exactly what aiohttp.test_utils.make_mocked_request's
# own writer= parameter exists for -- aiohttp's own test suite drives
# streaming handlers the same way. Not a zBots-invented mechanism, and it
# means every event the real handler produces (tool progress, reasoning,
# the final assistant.completed/run.completed bookkeeping) reaches the
# client exactly as upstream built it; zBots only consumes the frames the
# frontend already knows how to render (assistant.delta) and ignores the
# rest, so richer events later light up automatically with no wire-format
# changes needed here.
# --------------------------------------------------------------------------


class _QueueStreamWriter:
    """Stand-in for aiohttp's real StreamWriter (the AbstractStreamWriter
    protocol a Request/Response need) -- captures every write() into an
    asyncio.Queue instead of a transport, so a caller can consume the real
    handler's output live as it's produced.
    """

    def __init__(self) -> None:
        self.queue: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self.output_size = 0
        # StreamResponse._prepare_headers reads/sets this directly (no
        # Content-Length on an SSE response -> it stays None -> aiohttp
        # decides to chunk) and calls enable_chunking() when it does; both
        # are real-writer bookkeeping for wire-level chunked-transfer
        # framing zBots never emits (write() below forwards raw chunk
        # bytes untouched), so both are no-ops here.
        self.length: Optional[int] = None

    async def write_headers(self, status_line: str, headers: Any) -> None:
        # Headers never reach a real socket here -- main.py's own
        # StreamingResponse sets the Content-Type the client actually sees.
        pass

    def send_headers(self) -> None:
        pass

    def enable_chunking(self) -> None:
        pass

    async def write(self, chunk: bytes, *args: Any, **kwargs: Any) -> None:
        data = bytes(chunk)
        self.output_size += len(data)
        await self.queue.put(data)

    async def write_eof(self, chunk: bytes = b"") -> None:
        if chunk:
            await self.write(chunk)
        await self.queue.put(None)

    async def drain(self) -> None:
        pass


def _process_sse_frame(raw: bytes, sse_frame_fn) -> tuple[bytes, bool]:
    """Inspect one already-framed SSE chunk from the real streaming
    handler; return (possibly-rewritten frame, is_rollover_worthy).

    Touches/inspects assistant.delta and assistant.completed -- the two
    event types carrying model-generated free text (delta vs. content
    respectively); run.started/tool.progress/done/keepalive comments and
    everything else pass through byte-identical, never flagged. Two
    independent things happen in one parse pass since both need the same
    decoded text:

    1. Branding-safety redaction (same scrub send_to_bot() applies to a
       full reply). Known, narrow gap on the delta side: a leaked phrase
       split exactly across two separate write() calls escapes this
       (each frame is scrubbed in isolation) -- no worse than doing
       nothing, since a blocking, non-streaming reply had no equivalent
       boundary to split across; buffering the whole reply just to close
       that gap would defeat the point of streaming it live.
    2. resilience.stale_model_lock_rolls_over's same detection, applied
       here because THIS failure mode never raises an event: error frame
       -- the underlying provider call succeeds (200) with its own
       rejection delivered as ordinary-looking reply text, so the
       existing "was this frame literally event: error" rollover check
       (see _run_stream_attempt) can't see it. Confirmed live TWICE, in
       two different shapes: switching the active provider mid-
       conversation left an existing session's stale locked model id
       streaming straight through as assistant.delta chunks the first
       time: a later session hit the same underlying bug but with zero
       real token streaming at all -- the whole rejection arrived in one
       assistant.completed frame instead, which the delta-only version of
       this check couldn't see, so it reached a real user unflagged.
       Checking both event shapes is what catches shape currently known
       to occur; a still-different future shape isn't ruled out.
    """
    if not raw.startswith(b"event:"):
        return raw, False
    try:
        import json as _json

        text = raw.decode("utf-8")
        header_line, _, rest = text.partition("\n")
        event_name = header_line.split(":", 1)[1].strip()
        if event_name not in ("assistant.delta", "assistant.completed"):
            return raw, False
        data_line = rest.strip()
        if not data_line.startswith("data:"):
            return raw, False
        payload = _json.loads(data_line[len("data:") :].strip())
        field = "delta" if event_name == "assistant.delta" else "content"
        text_value = payload.get(field)
        if not isinstance(text_value, str) or not text_value:
            return raw, False
        is_stale_lock = bool(resilience._STALE_MODEL_LOCK_RE.match(text_value.strip()))
        payload[field] = persona.redact_branding_leaks(text_value)
        return sse_frame_fn(payload, event=event_name, ensure_ascii=False), is_stale_lock
    except Exception:
        # Never let this break the stream itself -- worst case is the
        # same unredacted frame the caller already had, undetected.
        return raw, False


async def _run_stream_attempt(profile: str, session_id: str, message: str, headers: dict):
    """One real streaming attempt against session_id. Yields
    (sse_frame_bytes, was_error) pairs live as the real handler produces
    them -- was_error is True on the handler's own `event: error` frame
    (if any), or on an assistant.delta frame whose text is actually a
    stale-model-lock rejection wearing a normal-looking delta (see
    _process_sse_frame); every other frame reports False.
    """
    from aiohttp.base_protocol import BaseProtocol
    from aiohttp.streams import StreamReader
    from aiohttp.test_utils import make_mocked_request
    from multidict import CIMultiDict
    import json as _json

    from gateway.platforms.api_server import _sse_frame

    body_bytes = _json.dumps({"message": message}).encode("utf-8")
    request_headers = CIMultiDict(headers)
    request_headers["Content-Type"] = "application/json"
    request_headers["Content-Length"] = str(len(body_bytes))
    payload = StreamReader(BaseProtocol(loop=None), limit=2**20, loop=None)
    payload.feed_data(body_bytes)
    payload.feed_eof()

    writer = _QueueStreamWriter()
    adapter = _get_adapter()
    await _ensure_mcp_tools_discovered()
    request = make_mocked_request(
        "POST",
        f"/api/sessions/{session_id}/chat/stream",
        headers=request_headers,
        payload=payload,
        match_info={"session_id": session_id},
        writer=writer,
    )

    async def _run() -> None:
        try:
            with _profile_scope(profile):
                await adapter._handle_session_chat_stream(request)
        except Exception as exc:
            await writer.queue.put(
                _sse_frame({"message": str(exc)}, event="error", ensure_ascii=False)
            )
        finally:
            await writer.queue.put(None)

    task = asyncio.create_task(_run())
    try:
        while True:
            chunk = await writer.queue.get()
            if chunk is None:
                break
            frame, is_stale_lock = _process_sse_frame(chunk, _sse_frame)
            yield frame, is_stale_lock or chunk.startswith(b"event: error\n")
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except Exception:
            pass


async def stream_to_bot(
    profile: str, message: str, api_server_key: str, active_session_id: Optional[str]
) -> tuple[dict, Any]:
    """Real per-token streaming: return (session_state, async_iterator_of_sse_bytes).

    session_state is a plain {"session_id": ...} dict the caller re-reads
    AFTER fully draining the iterator (not before -- a rollover mid-stream,
    see below, can change which session actually ended up serving the
    reply). Session lookup/creation mirrors send_to_bot()'s own
    _ensure_bot_chat_session call.

    Real bug found live, and this is the fix: a session's SECOND message
    onward reliably fails with `event: error` / "No LLM provider
    configured" whenever the active model routes through a custom
    endpoint (providers.<id> in config.yaml -- confirmed for BOTH zBots'
    own self-hosted "zbots" entry and a freshly-added one, so this isn't
    specific to any one provider). Root cause, traced live: the engine
    persists a session-level model lock after its first reply, and a
    custom endpoint's lock round-trips through that as the bare string
    "custom" -- enough to know it WAS a custom endpoint, not enough to
    know WHICH one, so credential resolution on the next message has
    nothing to authenticate with. A session's first-ever message never
    hits this (nothing stored yet to misresolve). send_to_bot()'s existing
    rollover logic has been silently absorbing this the entire time this
    session's chat has been in use -- every reply after the first in a
    conversation was actually a fresh, silently-rolled-over session
    underneath, not a continuation of the same one, which is why it was
    never noticed as a visible failure. This is that same rollover
    protection, adapted for a live stream: since the frontend only acts on
    assistant.delta events (see app.js's streamBotReply -- run.started/
    message.started/error/done are already inert to it), attempt 1's
    setup/error frames can be forwarded live with zero visible effect, and
    a rolled-over attempt 2 starting immediately after looks identical to
    one continuous stream. Retried once, matching send_to_bot()'s own
    single-retry policy -- if attempt 2 also errors, its frames still reach
    the client (nothing left to fall back to).

    The actual hermes-agent bug (session-lock serialization losing which
    custom endpoint it was) is not fixed here -- fixing that means tracing
    _persist_session_runtime_lock/_stored_session_model inside the
    vendored engine itself, a real change to make upstream, not a zBots
    workaround to reach for. Rollover was already the established pattern
    for exactly this class of failure; this keeps the streaming path
    consistent with it instead of inventing a second mechanism.
    """
    headers = _api_headers(api_server_key)
    session_id, all_sessions = await _ensure_bot_chat_session(profile, headers, active_session_id)
    state = {"session_id": session_id}

    async def _chunks():
        saw_error = False
        async for frame, was_error in _run_stream_attempt(profile, state["session_id"], message, headers):
            yield frame
            saw_error = saw_error or was_error
        if saw_error:
            new_sid = await _roll_over_bot_session(profile, headers, all_sessions)
            state["session_id"] = new_sid
            async for frame, _was_error in _run_stream_attempt(profile, new_sid, message, headers):
                yield frame

    return state, _chunks()


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
        # Deliberately NOT wrapped in _profile_scope here -- real bug
        # found live: for any non-default profile, _handle_session_messages
        # returns zero messages while scoped to that profile, even for a
        # session provably owned by it (confirmed: the same session_id,
        # same call, unscoped, returns the real messages correctly).
        # _handle_list_sessions and session/chat creation all work fine
        # scoped; this is specific to the message-read handler, and only
        # for secondary profiles -- matches the same class of bug already
        # documented elsewhere in the vendored engine around multiplex/
        # secondary-profile scoping (see agent.auxiliary_client's
        # _scoped_key_env). A session is looked up by its own globally
        # unique id, not by profile, so dropping the scope here doesn't
        # risk reading the wrong session -- it just stops an internal
        # ownership check that appears to only resolve correctly for the
        # default profile.
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
