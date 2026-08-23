"""Tests for the bot-supervisor MCP server's non-trivial logic
(backend/supervisor_mcp.py). The tool functions themselves are thin HTTP
wrappers (see the file's own docstrings) -- what's actually worth testing
directly is _run_delegated_task, the fire-and-forget background piece
delegate_task() kicks off, since a bug there fails silently (no caller is
ever waiting on it to raise).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import supervisor_mcp as sup


def _fake_response(json_body):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    return resp


@pytest.mark.asyncio
async def test_delegated_task_success_reports_the_real_reply_to_the_caller():
    calls = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            if "worker" in url:
                return _fake_response({"reply": "the real answer"})
            return _fake_response({"reply": "ok"})

    with patch.object(sup.httpx, "AsyncClient", lambda **kw: _FakeClient()):
        await sup._run_delegated_task("task-1", "supervisor", "worker", "do the thing")

    # First call is the actual delegated work, second is the report-back.
    assert calls[0] == (f"{sup.BOTS_UI_BASE}/bots/worker/messages", {"text": "do the thing"})
    report_url, report_body = calls[1]
    assert report_url == f"{sup.BOTS_UI_BASE}/bots/supervisor/messages"
    assert "task-1" in report_body["text"]
    assert "worker" in report_body["text"]
    assert "the real answer" in report_body["text"]


@pytest.mark.asyncio
async def test_delegated_task_failure_still_reports_back_instead_of_vanishing():
    calls = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            if "worker" in url:
                raise RuntimeError("worker is unreachable")
            return _fake_response({"reply": "ok"})

    with patch.object(sup.httpx, "AsyncClient", lambda **kw: _FakeClient()):
        await sup._run_delegated_task("task-2", "supervisor", "worker", "do the thing")

    # Only the report-back call should have succeeded; it must still
    # happen even though the delegated work itself raised.
    assert len(calls) == 2
    report_url, report_body = calls[1]
    assert report_url == f"{sup.BOTS_UI_BASE}/bots/supervisor/messages"
    assert "failed" in report_body["text"]


@pytest.mark.asyncio
async def test_delegate_task_tool_returns_immediately_without_waiting_for_the_worker():
    # The whole point is not blocking -- simulate a worker call that would
    # hang if delegate_task ever awaited it directly.
    release = asyncio.Event()

    async def _slow_post(url, json):
        if "worker" in url:
            await release.wait()
        return _fake_response({"reply": "ok"})

    async def _get(url):
        return _fake_response([{"name": "worker"}])

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        get = staticmethod(_get)
        post = staticmethod(_slow_post)

    with patch.object(sup.httpx, "AsyncClient", lambda **kw: _FakeClient()):
        result = await asyncio.wait_for(
            sup.delegate_task(from_bot="supervisor", to_bot="worker", task="research something"),
            timeout=2,
        )

    assert result["status"] == "delegated"
    assert result["to_bot"] == "worker"
    assert "task_id" in result

    # Clean up: let the background task finish and settle so the test
    # doesn't leak a pending task into the next one.
    release.set()
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# _require_bot -- the existence check. Real bug found live: messaging a name
# that isn't a real bot used to silently "succeed" (POST /bots/{name}/messages
# auto-creates a session under any name and answers anyway, no actual bot
# behind it), so a typo'd or hallucinated name looked like a real reply
# instead of a clear failure.
# ---------------------------------------------------------------------------

def _roster_client(names):
    async def _get(url):
        return _fake_response([{"name": n} for n in names])

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        get = staticmethod(_get)

    return _FakeClient()


@pytest.mark.asyncio
async def test_require_bot_passes_silently_for_a_real_name():
    with patch.object(sup.httpx, "AsyncClient", lambda **kw: _roster_client(["worker", "default"])):
        await sup._require_bot("worker")  # must not raise


@pytest.mark.asyncio
async def test_require_bot_suggests_a_close_match():
    with patch.object(sup.httpx, "AsyncClient", lambda **kw: _roster_client(["mandy", "default"])):
        with pytest.raises(sup.BotNotFound) as exc_info:
            await sup._require_bot("mandi")
    assert "mandy" in str(exc_info.value)


@pytest.mark.asyncio
async def test_require_bot_reports_no_match_when_nothing_close():
    with patch.object(sup.httpx, "AsyncClient", lambda **kw: _roster_client(["default"])):
        with pytest.raises(sup.BotNotFound) as exc_info:
            await sup._require_bot("zzzzzz")
    assert "no similarly" in str(exc_info.value)
