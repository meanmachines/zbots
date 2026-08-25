"""Unit tests for engine.py's http-transport primitives (originally Phase 1
of the native-hermes migration; _bot_base itself was later superseded by
the per-bot dedicated worker process architecture -- see
bot_processes.py's own module docstring) -- _call_handler_http and
_run_stream_attempt_http. Every real socket is replaced with
httpx.MockTransport, matching how test_backend.py already mocks the wire
boundary for the embedded path's own callers.

These pin two things the embedded path never had to worry about:
1. _bot_base resolves each profile to that bot's own dedicated worker
   port (bot_processes.get_port), including "default" -- confirmed here
   so a future change to _bot_base can't silently regress back to a
   shared/URL-prefix scheme without a test noticing.
2. a real chunked response can split one SSE frame across more than one
   read (the in-memory queue the embedded path used never could) --
   _run_stream_attempt_http has to reassemble on blank-line boundaries
   before handing a frame to _process_sse_frame.
"""

import asyncio
import json
import sys
import types

import httpx

from backend import engine


def _use_http_transport(monkeypatch, handler):
    """Route every httpx.AsyncClient engine.py constructs through a
    MockTransport instead of a real socket, and flip the transport flag
    the same way ZBOTS_CHAT_TRANSPORT=http would at import time.
    """
    monkeypatch.setattr(engine, "_CHAT_TRANSPORT", "http")
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(engine.httpx, "AsyncClient", _client)


# ---------------------------------------------------------------------------
# _bot_base itself -- every profile, including "default", resolves through
# bot_processes.get_port now, not a shared-gateway URL.
# ---------------------------------------------------------------------------

def test_bot_base_uses_bot_processes_get_port(monkeypatch):
    monkeypatch.setattr(engine.bot_processes, "get_port", lambda profile: 8765)
    assert engine._bot_base("coder") == "http://127.0.0.1:8765"


def test_bot_base_does_not_special_case_default(monkeypatch):
    seen_profiles = []

    def fake_get_port(profile):
        seen_profiles.append(profile)
        return 8700

    monkeypatch.setattr(engine.bot_processes, "get_port", fake_get_port)
    assert engine._bot_base("default") == "http://127.0.0.1:8700"
    assert seen_profiles == ["default"]


# ---------------------------------------------------------------------------
# _call_handler_http / _call_handler dispatch
# ---------------------------------------------------------------------------

def test_non_default_profile_resolves_to_its_own_worker_port(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"data": []})

    _use_http_transport(monkeypatch, handler)
    monkeypatch.setattr(engine.bot_processes, "get_port", lambda profile: 8712)

    async def _run():
        return await engine._call_handler(
            "_handle_list_sessions",
            profile="coder",
            method="GET",
            path="/api/sessions",
            query={"limit": 200},
            headers={"Authorization": "Bearer x"},
        )

    status, body = asyncio.run(_run())
    assert status == 200
    assert body == {"data": []}
    assert seen["method"] == "GET"
    assert seen["url"] == "http://127.0.0.1:8712/api/sessions?limit=200"


def test_default_profile_also_resolves_to_its_own_worker_port(monkeypatch):
    # "default" is no longer special-cased onto a shared gateway URL --
    # every bot, "default" included, runs its own dedicated worker (see
    # bot_processes.py's own module docstring for why).
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    _use_http_transport(monkeypatch, handler)
    monkeypatch.setattr(engine.bot_processes, "get_port", lambda profile: 8700)

    async def _run():
        return await engine._call_handler("_h", profile="default", method="GET", path="/api/sessions")

    status, body = asyncio.run(_run())
    assert status == 200
    assert seen["url"] == "http://127.0.0.1:8700/api/sessions"


def test_json_body_and_headers_are_forwarded(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"session": {"id": "s1"}})

    _use_http_transport(monkeypatch, handler)

    async def _run():
        return await engine._call_handler(
            "_handle_create_session",
            profile="butler",
            method="POST",
            path="/api/sessions",
            json_body={"title": "[Bots UI] butler", "source": "bots_ui"},
            headers={"Authorization": "Bearer secret"},
        )

    status, body = asyncio.run(_run())
    assert status == 201
    assert body["session"]["id"] == "s1"
    assert seen["body"] == {"title": "[Bots UI] butler", "source": "bots_ui"}
    assert seen["auth"] == "Bearer secret"


def test_chat_call_timeout_is_hours_not_seconds(monkeypatch):
    # Real bug found live: this was a flat 120.0s, which killed a real
    # multi-hour agentic turn mid-run and triggered a destructive rollover
    # (see engine.py's own comment on _call_handler_http for the full
    # incident). Pins the fix at the boundary a future regression would
    # most likely reintroduce -- accidentally shrinking it back down.
    seen = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    def _client(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(engine, "_CHAT_TRANSPORT", "http")
    monkeypatch.setattr(engine.httpx, "AsyncClient", _client)

    asyncio.run(engine._call_handler("_h", profile="coder", method="GET", path="/api/sessions"))

    timeout = seen["timeout"]
    assert timeout.connect == 10.0
    assert timeout.read >= 3600.0  # at least an hour -- a real safety net, not a short cap


def test_transport_failure_becomes_a_500_not_an_exception(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _use_http_transport(monkeypatch, handler)

    async def _run():
        return await engine._call_handler("_h", profile="default", method="GET", path="/api/sessions")

    status, body = asyncio.run(_run())
    # Mirrors _call_handler_embedded's own contract: callers here (and
    # resilience.evaluate) check `status >= 400`, never expect a raised
    # exception to escape this function.
    assert status == 500
    assert "error" in body


def test_default_transport_stays_embedded_when_flag_unset(monkeypatch):
    # ZBOTS_CHAT_TRANSPORT defaults to "embedded" -- _use_http() must stay
    # False unless something explicitly sets the module-level flag to
    # "http", matching the plan's "safe fallback" rollout requirement.
    monkeypatch.setattr(engine, "_CHAT_TRANSPORT", "embedded")
    assert engine._use_http() is False


# ---------------------------------------------------------------------------
# steer_run -- POST /v1/runs/{run_id}/steer, the real native mechanism for
# redirecting a bot while it's still actively working (see its own
# docstring in engine.py for the real steering incident this was built
# from, found live on the user's own hermes-agent desktop app).
# ---------------------------------------------------------------------------

def test_steer_run_returns_not_accepted_under_embedded_transport(monkeypatch):
    monkeypatch.setattr(engine, "_CHAT_TRANSPORT", "embedded")
    result = asyncio.run(engine.steer_run("coder", "run_abc", "keep going", "key"))
    assert result["accepted"] is False


def test_steer_run_posts_to_the_bots_own_worker(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"object": "hermes.run.steer", "run_id": "run_abc", "accepted": True})

    _use_http_transport(monkeypatch, handler)
    monkeypatch.setattr(engine.bot_processes, "get_port", lambda profile: 9001)

    result = asyncio.run(engine.steer_run("coder", "run_abc", "keep going", "key"))
    assert result == {"object": "hermes.run.steer", "run_id": "run_abc", "accepted": True}
    assert seen["url"] == "http://127.0.0.1:9001/v1/runs/run_abc/steer"
    assert seen["body"] == {"input": "keep going"}


def test_steer_run_reports_not_accepted_on_a_rejected_run(monkeypatch):
    # A real, expected case -- not an error: the run finished in the gap
    # between the client seeing it as "still active" and the steer
    # actually landing. The real API's own 409 run_not_accepting_steer.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"message": "Run is not currently accepting steer input"}})

    _use_http_transport(monkeypatch, handler)
    monkeypatch.setattr(engine.bot_processes, "get_port", lambda profile: 9001)

    result = asyncio.run(engine.steer_run("coder", "run_abc", "keep going", "key"))
    assert result["accepted"] is False


def test_steer_run_never_raises_on_a_transport_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _use_http_transport(monkeypatch, handler)
    monkeypatch.setattr(engine.bot_processes, "get_port", lambda profile: 9001)

    result = asyncio.run(engine.steer_run("coder", "run_abc", "keep going", "key"))
    assert result["accepted"] is False


# ---------------------------------------------------------------------------
# _run_stream_attempt_http -- real chunk-boundary reassembly
# ---------------------------------------------------------------------------

class _ChunkedStream(httpx.AsyncByteStream):
    """Delivers pre-split byte chunks exactly as given -- unlike a single
    Response(content=...), this actually exercises a frame boundary
    landing mid-chunk, the scenario a real chunked HTTP response can hit
    but the embedded path's in-memory queue never could (see this file's
    own module docstring).
    """

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


def _sse_frame_stub(data, *, event=None, ensure_ascii=True):
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=ensure_ascii)}\n\n".encode()


def _stub_vendored_sse_frame(monkeypatch):
    """_run_stream_attempt_http does `from gateway.platforms.api_server
    import _sse_frame` internally -- real vendored code, deliberately not
    injectable (see engine.py's own module docstring: reusing the real
    handler/formatting is the whole point, not reimplementing it). CI's
    sparse checkout never fetches vendor/hermes-agent (~170MB, see
    test_engine_streaming.py's own docstring and .github/workflows/ci.yml),
    so this stubs the three-level module path in sys.modules instead of
    requiring a real vendor checkout just to unit-test frame reassembly.
    """
    gateway_mod = types.ModuleType("gateway")
    platforms_mod = types.ModuleType("gateway.platforms")
    api_server_mod = types.ModuleType("gateway.platforms.api_server")
    api_server_mod._sse_frame = _sse_frame_stub
    gateway_mod.platforms = platforms_mod
    platforms_mod.api_server = api_server_mod
    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms_mod)
    monkeypatch.setitem(sys.modules, "gateway.platforms.api_server", api_server_mod)


def test_reassembles_an_sse_frame_split_across_two_chunks(monkeypatch):
    _stub_vendored_sse_frame(monkeypatch)
    full_frame = _sse_frame_stub({"delta": "hello"}, event="assistant.delta")
    split_at = 10
    chunks = [full_frame[:split_at], full_frame[split_at:]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkedStream(chunks))

    _use_http_transport(monkeypatch, handler)

    async def _run():
        out = []
        async for frame, was_error in engine._run_stream_attempt_http(
            "default", "session-1", "hi", {"Authorization": "Bearer x"}
        ):
            out.append((frame, was_error))
        return out

    frames = asyncio.run(_run())
    assert len(frames) == 1
    frame, was_error = frames[0]
    assert was_error is False
    assert json.loads(frame.decode().split("data: ", 1)[1]) == {"delta": "hello"}


def test_multiple_frames_in_one_chunk_are_split_correctly(monkeypatch):
    _stub_vendored_sse_frame(monkeypatch)
    frame_a = _sse_frame_stub({"delta": "a"}, event="assistant.delta")
    frame_b = _sse_frame_stub({}, event="run.completed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkedStream([frame_a + frame_b]))

    _use_http_transport(monkeypatch, handler)

    async def _run():
        return [f async for f, _ in engine._run_stream_attempt_http("default", "s1", "hi", {})]

    frames = asyncio.run(_run())
    assert frames == [frame_a, frame_b]


def test_a_status_error_response_yields_one_error_frame(monkeypatch):
    _stub_vendored_sse_frame(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"internal error")

    _use_http_transport(monkeypatch, handler)

    async def _run():
        out = []
        async for frame, was_error in engine._run_stream_attempt_http("default", "s1", "hi", {}):
            out.append((frame, was_error))
        return out

    frames = asyncio.run(_run())
    assert len(frames) == 1
    frame, was_error = frames[0]
    assert was_error is True
    assert b"internal error" in frame
