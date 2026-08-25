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
# _sync_profile_provider -- real bug found live (Phase 1 http-transport
# testing, see the project plan): under gateway.multiplex_profiles, a
# secondary profile's own config.yaml never inherits the default profile's
# providers: block, only a model: reference to a provider name only the
# default profile actually defines. Worked by accident under the embedded
# chat transport (one shared adapter built from the default profile's
# config, reused regardless of scope); the real gateway (http transport)
# resolves providers strictly per-profile and 404s with "Unknown provider"
# for any bot whose own config never got the definition copied in.
# ---------------------------------------------------------------------------

def test_sync_profile_provider_copies_the_real_definition_into_the_profile(monkeypatch):
    calls = []

    async def fake_dash_get(path, **kwargs):
        assert path == "/api/config"
        return {"providers": {"sglang-thor": {"base_url": "http://thor:30000/v1", "model": "qwen3.8-27b"}}}

    async def fake_dash_send(method, path, body=None, query=None):
        calls.append((method, path, body, query))
        return {}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    monkeypatch.setattr(m, "dash_send", fake_dash_send)

    asyncio.run(m._sync_profile_provider("coder", "sglang-thor"))

    assert len(calls) == 1
    method, path, body, query = calls[0]
    assert method == "PUT"
    assert path == "/api/config"
    assert body == {"config": {"providers": {"sglang-thor": {"base_url": "http://thor:30000/v1", "model": "qwen3.8-27b"}}}}
    assert query == {"profile": "coder"}


def test_sync_profile_provider_skips_reserved_builtin_providers(monkeypatch):
    # openrouter/auto/custom (and every hermes-agent-recognized built-in
    # name) resolve through native code paths and env-var keys, not a
    # providers: entry -- nothing to copy, and _reserved_provider_ids
    # itself is what create_bot/update_bot already use to refuse letting a
    # user name a custom endpoint one of these in the first place.
    dash_get_called = []
    monkeypatch.setattr(m, "dash_get", AsyncMock(side_effect=lambda *a, **k: dash_get_called.append(1)))
    monkeypatch.setattr(m, "_reserved_provider_ids", lambda: frozenset({"openrouter", "auto", "custom"}))

    asyncio.run(m._sync_profile_provider("default", "openrouter"))
    assert dash_get_called == []


def test_sync_profile_provider_is_a_noop_with_no_provider(monkeypatch):
    dash_get_called = []
    monkeypatch.setattr(m, "dash_get", AsyncMock(side_effect=lambda *a, **k: dash_get_called.append(1)))
    asyncio.run(m._sync_profile_provider("default", None))
    assert dash_get_called == []


def test_sync_profile_provider_is_best_effort_on_a_missing_catalog_entry(monkeypatch):
    # The default profile's own config not having the provider shouldn't
    # happen (see this function's own docstring) but must not raise --
    # bot creation/update already succeeded by the time this runs.
    async def fake_dash_get(path, **kwargs):
        return {"providers": {}}

    send_calls = []
    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    monkeypatch.setattr(m, "dash_send", AsyncMock(side_effect=lambda *a, **k: send_calls.append(1)))

    asyncio.run(m._sync_profile_provider("coder", "sglang-thor"))
    assert send_calls == []


def test_sync_profile_provider_swallows_a_dashboard_failure(monkeypatch):
    # Best-effort: a transport failure here must not propagate and fail
    # the bot creation/update request it's called from.
    async def boom(path, **kwargs):
        raise m.httpx.ConnectError("no route")

    monkeypatch.setattr(m, "dash_get", boom)
    asyncio.run(m._sync_profile_provider("coder", "sglang-thor"))  # must not raise


# ---------------------------------------------------------------------------
# _provision_profile_provider_secret -- real bug found live, right after
# fixing the providers: sync above and re-testing: syncing the block was
# enough for zBots' own unauthenticated sglang endpoints, but a synced
# deepseek-flash entry still 401'd for any non-default bot -- key_env only
# resolves through the multiplex profile's OWN .env (agent/secret_scope.py),
# never this container's process env, for a scoped profile.
# ---------------------------------------------------------------------------

def test_provisions_the_key_env_value_from_the_root_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY=sk-real-value\n", encoding="utf-8")
    profile_dir = tmp_path / "profiles" / "hydration-reminder"
    profile_dir.mkdir(parents=True)
    (profile_dir / ".env").write_text("API_SERVER_KEY=abc\n", encoding="utf-8")

    m._provision_profile_provider_secret(
        "hydration-reminder", {"key_env": "HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY"}
    )

    content = (profile_dir / ".env").read_text(encoding="utf-8")
    assert "HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY=sk-real-value" in content
    assert "API_SERVER_KEY=abc" in content  # existing content preserved


def test_is_a_noop_when_the_key_already_exists_in_the_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY=sk-real-value\n", encoding="utf-8")
    profile_dir = tmp_path / "profiles" / "hydration-reminder"
    profile_dir.mkdir(parents=True)
    (profile_dir / ".env").write_text("HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY=sk-already-here\n", encoding="utf-8")

    m._provision_profile_provider_secret(
        "hydration-reminder", {"key_env": "HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY"}
    )

    content = (profile_dir / ".env").read_text(encoding="utf-8")
    # Never overwritten -- idempotent, same convention as
    # _provision_profile_api_server_key.
    assert content.count("HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY=") == 1
    assert "sk-already-here" in content


def test_is_a_noop_for_an_unauthenticated_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_dir = tmp_path / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    (profile_dir / ".env").write_text("", encoding="utf-8")

    m._provision_profile_provider_secret("coder", {"base_url": "http://thor:30000/v1"})  # no key_env

    assert (profile_dir / ".env").read_text(encoding="utf-8") == ""


def test_is_best_effort_when_the_root_env_has_no_such_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("SOME_OTHER_KEY=x\n", encoding="utf-8")
    profile_dir = tmp_path / "profiles" / "hydration-reminder"
    profile_dir.mkdir(parents=True)
    (profile_dir / ".env").write_text("", encoding="utf-8")

    m._provision_profile_provider_secret(
        "hydration-reminder", {"key_env": "HERMES_CUSTOM_DEEPSEEK_FLASH_API_KEY"}
    )  # must not raise

    assert (profile_dir / ".env").read_text(encoding="utf-8") == ""


def test_create_bot_syncs_the_provider_into_the_new_profile(client, monkeypatch):
    sync_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="", provider="prov-a", model="model-a")]

    async def fake_sync(profile, provider):
        sync_calls.append((profile, provider))

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_sync_profile_provider", fake_sync)

    resp = client.post("/bots", json={"name": "alpha", "title": "Alpha", "description": "d"})
    assert resp.status_code == 200
    assert sync_calls == [("alpha", "default-prov")]


def test_update_bot_syncs_the_provider_on_a_model_change(client, monkeypatch):
    sync_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_lock(profile, provider, model):
        pass

    async def fake_sync(profile, provider):
        sync_calls.append((profile, provider))

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_lock_active_session_model", fake_lock)
    monkeypatch.setattr(m, "_sync_profile_provider", fake_sync)

    resp = client.patch("/bots/alpha", json={"provider": "prov-b", "model": "model-b"})
    assert resp.status_code == 200
    assert sync_calls == [("alpha", "prov-b")]


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
    # The real, vendored hermes_cli package -- not mocked -- since the
    # whole point of this function is to stay in sync with hermes-agent's
    # own registry rather than duplicating it. Everything else in this
    # section mocks it (see that test/CI's own note above): CI's sparse
    # checkout deliberately never fetches vendor/hermes-agent, so a real
    # import here only works where the checkout is full (local dev, and
    # any live-container check) -- skip cleanly where it isn't.
    pytest.importorskip("hermes_cli", reason="only present in a full checkout, not CI's sparse one")
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
    monkeypatch.setattr(m, "_reserved_provider_ids", lambda: frozenset({"deepseek", "qwen"}))

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
    monkeypatch.setattr(m, "_reserved_provider_ids", lambda: frozenset({"deepseek", "qwen"}))
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
