"""Backend tests with the Hermes dashboard/api_server fully mocked.

The point of this suite is to lock in zBots' own behavior -- auth, roster
shaping, chat resilience, group edits -- without needing a live Hermes
instance. Everything that would cross the wire is patched at the backend.main
boundary.
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

import backend.main as m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile(name, **overrides):
    base = {
        "name": name,
        "display_name": None,
        "description": f"{name} description",
        "model": "model-a",
        "provider": "prov-a",
        "gateway_running": True,
    }
    base.update(overrides)
    return base


def _fake_response(status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = b"{}"
    if payload is not None:
        resp.json.return_value = payload
    return resp


class _FakeHTTPX:
    """Async-context-manager stand-in for httpx.AsyncClient in send_to_bot."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Health / readiness / version
# ---------------------------------------------------------------------------

def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_ready_ok(client, monkeypatch):
    # /ready also checks the embedded engine can construct (see engine.py) --
    # no real Hermes profile exists in the test environment, so that check
    # is mocked out here the same way dashboard reachability is, to isolate
    # what this test actually exercises: readiness given a reachable dashboard.
    monkeypatch.setattr(m._engine, "_get_adapter", MagicMock())
    monkeypatch.setattr(m._dash_client, "get", AsyncMock(return_value=_fake_response(200, {})))
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_ready_unreachable(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise m.httpx.ConnectError("no route")

    monkeypatch.setattr(m._dash_client, "get", boom)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def test_version(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    assert "sha" in resp.json()


# ---------------------------------------------------------------------------
# Optional API-key auth
# ---------------------------------------------------------------------------

def test_api_key_auth_required_when_enabled(client, monkeypatch):
    m.BOTS_UI_API_KEY = "s3cret"
    monkeypatch.setattr(m, "dash_get", AsyncMock(return_value={"profiles": []}))
    assert client.get("/roster").status_code == 401
    assert client.get("/health").status_code == 200
    ok = client.get("/roster", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_api_key_auth_disabled_by_default(client, monkeypatch):
    m.BOTS_UI_API_KEY = ""
    monkeypatch.setattr(m, "dash_get", AsyncMock(return_value={"profiles": []}))
    assert client.get("/roster").status_code == 200


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def test_roster_orders_by_activity_and_hides_hidden(client, monkeypatch):
    async def fake_dash_get(path, **kwargs):
        assert path == "/api/profiles"
        return {"profiles": [_profile("old"), _profile("new")]}

    async def fake_activity(profile):
        if profile == "new":
            return {"preview": "hi", "last_active": 200.0, "is_active": True}
        return {"preview": "", "last_active": 100.0, "is_active": False}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    monkeypatch.setattr(m, "get_bot_activity", fake_activity)
    rows = client.get("/roster").json()
    assert [r["name"] for r in rows] == ["new", "old"]
    assert rows[0]["is_active"] is True
    assert rows[0]["preview"] == "hi"


def test_roster_uses_local_title_when_profile_has_none(client, monkeypatch, state_file):
    state_file.write_text(json.dumps({"titles": {"my-bot": "My Custom Bot"}}))
    monkeypatch.setattr(m, "dash_get", AsyncMock(return_value={"profiles": [_profile("my-bot")]}))
    monkeypatch.setattr(m, "get_bot_activity", AsyncMock(return_value={}))
    rows = client.get("/roster").json()
    assert rows[0]["title"] == "My Custom Bot"


# ---------------------------------------------------------------------------
# Bot CRUD
# ---------------------------------------------------------------------------

def test_create_bot_forces_explicit_provider_model(client, monkeypatch):
    created = {}

    async def fake_dash_send(method, path, body):
        created["body"] = body
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="", provider="prov-a", model="model-a")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)

    resp = client.post("/bots", json={"name": "alpha", "title": "Alpha", "description": "d"})
    assert resp.status_code == 200
    assert created["body"]["provider"] == "default-prov"
    assert created["body"]["model"] == "default-model"


def test_update_bot_locks_active_session_model(client, monkeypatch):
    lock_calls = []

    async def fake_dash_send(method, path, body):
        return {}

    async def fake_lock(profile, provider, model):
        lock_calls.append((profile, provider, model))

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_lock_active_session_model", fake_lock)

    resp = client.patch("/bots/alpha", json={"provider": "prov-b", "model": "model-b"})
    assert resp.status_code == 200
    assert lock_calls == [("alpha", "prov-b", "model-b")]


def test_get_bot_soul(client, monkeypatch):
    monkeypatch.setattr(m, "dash_get", AsyncMock(return_value={"content": "# Persona"}))
    resp = client.get("/bots/alpha/soul")
    assert resp.status_code == 200
    assert resp.json()["content"] == "# Persona"


# ---------------------------------------------------------------------------
# Chat resilience
# ---------------------------------------------------------------------------

# The 500-rollover and corrupted-reply-retry resilience logic these tests
# used to exercise here now lives inside engine.py (chat/sessions run
# in-process against the vendored engine, see engine.py's module
# docstring) -- already verified live against a real deployment, including
# the exact rollover and corruption scenarios these tests used to mock at
# the httpx layer. What's left for main.py's own send_to_bot to be
# responsible for is delegating to the embedded engine and persisting
# whatever session id comes back into local state, which is what these
# now test instead. A dedicated engine.py test module, mocking at the
# aiohttp-handler boundary (_call_handler) rather than httpx, is real
# follow-up work for covering the rollover/retry logic itself.

def test_send_to_bot_delegates_to_engine_and_returns_reply(monkeypatch):
    fake_engine_send = AsyncMock(return_value=("clean reply", "s2"))
    monkeypatch.setattr(m._engine, "send_to_bot", fake_engine_send)

    reply = asyncio.run(m.send_to_bot("alpha", "hello"))

    assert reply == "clean reply"
    fake_engine_send.assert_awaited_once()
    call_args = fake_engine_send.await_args.args
    assert call_args[0] == "alpha"
    assert call_args[1] == "hello"


def test_send_to_bot_persists_new_session_id_on_change(monkeypatch, state_file):
    if state_file.exists():
        state_file.unlink()
    monkeypatch.setattr(m._engine, "send_to_bot", AsyncMock(return_value=("after rollover", "s2")))

    asyncio.run(m.send_to_bot("alpha", "hello"))

    state = m._read_state()
    assert state["active_sessions"]["alpha"] == "s2"


def test_send_to_bot_skips_state_write_when_session_id_unchanged(monkeypatch, state_file):
    m._write_state({**m._default_state(), "active_sessions": {"alpha": "s1"}})
    monkeypatch.setattr(m._engine, "send_to_bot", AsyncMock(return_value=("same session", "s1")))
    write_spy = MagicMock(wraps=m._write_state)
    monkeypatch.setattr(m, "_write_state", write_spy)

    asyncio.run(m.send_to_bot("alpha", "hello"))

    write_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def _create_group(client):
    resp = client.post("/groups", json={"name": "Team", "members": ["alpha", "beta"]})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Provider self-service (POST /providers) -- collision guard.
#
# Real bug found live: a custom endpoint saved with name "deepseek" reused
# hermes-agent's own built-in "deepseek" provider slug, so the resolver
# routed every request through the built-in overlay (which expects
# DEEPSEEK_API_KEY) instead of the custom entry's own base_url/key_env --
# auth failed even though the user's key was saved correctly.
# ---------------------------------------------------------------------------

def test_reserved_provider_ids_includes_known_builtins_and_aliases():
    reserved = m._reserved_provider_ids()
    assert "deepseek" in reserved  # PROVIDER_REGISTRY entry
    assert "qwen" in reserved  # ALIASES entry (-> alibaba)


def test_custom_endpoint_id_mirrors_dashboard_slugify():
    assert m._custom_endpoint_id("DeepSeek Flash") == "deepseek-flash"
    assert m._custom_endpoint_id("") == "custom"


def test_save_provider_rejects_a_name_that_collides_with_a_builtin(client, monkeypatch):
    async def fake_dash_send(method, path, body):
        raise AssertionError("dash_send must not be called once the name collides")

    monkeypatch.setattr(m, "dash_send", fake_dash_send)

    resp = client.post(
        "/providers",
        json={"name": "deepseek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    )
    assert resp.status_code == 400
    assert "deepseek" in resp.json()["detail"]


def test_save_provider_accepts_a_non_colliding_name_and_invalidates_the_adapter(client, monkeypatch):
    async def fake_dash_send(method, path, body):
        return {"ok": True, "id": "deepseek-flash"}

    invalidated = []
    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m._engine, "invalidate_adapter", lambda: invalidated.append(True))

    resp = client.post(
        "/providers",
        json={"name": "deepseek-flash", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    )
    assert resp.status_code == 200
    assert invalidated == [True]


def test_group_update_rename_and_members(client):
    group = _create_group(client)
    resp = client.patch(f"/groups/{group['id']}", json={"name": "Renamed", "members": ["alpha", "gamma"]})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["members"] == ["alpha", "gamma"]


def test_group_update_rejects_single_member(client):
    group = _create_group(client)
    resp = client.patch(f"/groups/{group['id']}", json={"members": ["alpha"]})
    assert resp.status_code == 400


def test_group_send_feeds_transcript_to_later_rounds(client, monkeypatch):
    group = _create_group(client)
    send_mock = AsyncMock(side_effect=["alpha reply @beta", "beta reply"])
    monkeypatch.setattr(m, "send_to_bot", send_mock)

    resp = client.post(f"/groups/{group['id']}/messages", json={"text": "@alpha start"})
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2

    first_ctx = send_mock.call_args_list[0].args[1]
    second_ctx = send_mock.call_args_list[1].args[1]
    assert "start" in first_ctx
    assert "alpha reply @beta" in second_ctx


# ---------------------------------------------------------------------------
# Local state
# ---------------------------------------------------------------------------

def test_state_migration_stamps_version(state_file):
    state_file.write_text(json.dumps({"hidden": ["old-bot"]}))
    data = m._read_state()
    assert data["version"] == m.STATE_VERSION
    assert data["hidden"] == ["old-bot"]


def test_state_defaults_when_missing(state_file):
    assert m._read_state()["version"] == m.STATE_VERSION
    assert m._read_state()["groups"] == {}
