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
# _bot_base -- every profile, including "default", resolves through
# bot_processes.get_port now (its own dedicated worker process), not a
# shared-gateway URL or /p/<profile>/ prefix. Same duplication reasoning
# as engine.py's own copy (kept separate to avoid a circular import) --
# see bot_processes.py's own module docstring for why this exists.
# ---------------------------------------------------------------------------

def test_bot_base_uses_bot_processes_get_port(monkeypatch):
    monkeypatch.setattr(m.bot_processes, "get_port", lambda profile: 8765)
    assert m._bot_base("coder") == "http://127.0.0.1:8765"


def test_bot_base_does_not_special_case_default(monkeypatch):
    monkeypatch.setattr(m.bot_processes, "get_port", lambda profile: 8700)
    assert m._bot_base("default") == "http://127.0.0.1:8700"


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
# ensure_bot_process_running wiring -- create_bot/update_bot/delete_session
# all always do real HTTP against a bot's own worker (bot_processes.py),
# independent of engine.py's own ZBOTS_CHAT_TRANSPORT (see each call
# site's own comment in main.py) -- a currently-sleeping bot needs waking
# first or the call would fail against a port nothing is listening on.
# ---------------------------------------------------------------------------

def test_create_bot_wakes_the_new_bots_worker(client, monkeypatch):
    wake_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="", provider="prov-a", model="model-a")]

    async def fake_wake(profile):
        wake_calls.append(profile)
        return 8700

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake)

    resp = client.post("/bots", json={"name": "alpha", "title": "Alpha", "description": "d"})
    assert resp.status_code == 200
    assert wake_calls == ["alpha"]


def test_update_bot_wakes_the_bot_before_locking_its_model(client, monkeypatch):
    wake_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_lock(profile, provider, model):
        pass

    async def fake_wake(profile):
        wake_calls.append(profile)
        return 8700

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_lock_active_session_model", fake_lock)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake)

    resp = client.patch("/bots/alpha", json={"provider": "prov-b", "model": "model-b"})
    assert resp.status_code == 200
    assert wake_calls == ["alpha"]


def test_update_bot_does_not_wake_anything_on_a_non_model_change(client, monkeypatch):
    wake_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_wake(profile):
        wake_calls.append(profile)
        return 8700

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake)

    resp = client.patch("/bots/alpha", json={"description": "just a description change"})
    assert resp.status_code == 200
    assert wake_calls == []


def test_delete_session_wakes_the_bot_before_dialing_it(client, monkeypatch):
    wake_calls = []

    async def fake_wake(profile):
        wake_calls.append(profile)
        return 8700

    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake)
    monkeypatch.setattr(m.bot_processes, "get_port", lambda profile: 8700)

    async def fake_delete(*args, **kwargs):
        return _fake_response(200, {"ok": True})

    monkeypatch.setattr(m.httpx.AsyncClient, "delete", fake_delete)

    resp = client.delete("/sessions/sess-1?profile=alpha")
    assert resp.status_code == 200
    assert wake_calls == ["alpha"]


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


# ---------------------------------------------------------------------------
# Bot worker lifecycle -- _keep_warm_bots, the startup/shutdown handlers,
# and the idle-reap loop. See bot_processes.py's own module docstring and
# main.py's "Bot worker lifecycle" section comment for the reasoning: a
# bot's own cron scheduler only runs while ITS OWN worker is alive, so any
# bot with an enabled routine has to stay warm for that routine to ever
# fire; "default" is always warm (messaging connectors); everything else
# is on-demand, woken by a real chat request and reaped after inactivity.
# ---------------------------------------------------------------------------

def test_keep_warm_bots_always_includes_default(monkeypatch):
    async def fake_dash_get(path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "coder"]))
    assert "default" in warm
    assert "coder" not in warm


def test_keep_warm_bots_includes_a_bot_with_an_enabled_routine(monkeypatch):
    async def fake_dash_get(path, **kwargs):
        return {"data": [{"name": "[bot:coder] hydration-check", "enabled": True}]}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "coder", "botty"]))
    assert warm == {"default", "coder"}


def test_keep_warm_bots_ignores_a_disabled_routine(monkeypatch):
    async def fake_dash_get(path, **kwargs):
        return {"data": [{"name": "[bot:coder] hydration-check", "enabled": False}]}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "coder"]))
    assert warm == {"default"}


def test_keep_warm_bots_is_best_effort_on_a_dashboard_failure(monkeypatch):
    async def boom(path, **kwargs):
        raise m.httpx.ConnectError("no route")

    monkeypatch.setattr(m, "dash_get", boom)
    warm = asyncio.run(m._keep_warm_bots(["default", "coder"]))
    assert warm == {"default"}  # still gets the unconditional part


def test_startup_handler_is_a_noop_under_embedded_transport(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: False)
    spawn_calls = []
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=lambda p: spawn_calls.append(p)))
    asyncio.run(m._spawn_keep_warm_bots_and_start_reaper())
    assert spawn_calls == []


def test_startup_handler_spawns_every_keep_warm_bot(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: True)

    async def fake_get_roster(include_hidden=False):
        return [
            m.RosterEntry(name="default", title="Default", description=""),
            m.RosterEntry(name="coder", title="Coder", description=""),
        ]

    spawn_calls = []

    async def fake_wake(profile):
        spawn_calls.append(profile)
        return 8700

    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_keep_warm_bots", AsyncMock(return_value={"default"}))
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake)
    monkeypatch.setattr(m.asyncio, "create_task", lambda coro: coro.close())  # don't actually start the reap loop

    asyncio.run(m._spawn_keep_warm_bots_and_start_reaper())
    assert spawn_calls == ["default"]


def test_startup_handler_survives_one_bots_spawn_failure(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: True)

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="default", title="Default", description="")]

    async def fake_wake_that_fails(profile):
        raise RuntimeError("worker never became ready")

    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_keep_warm_bots", AsyncMock(return_value={"default"}))
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake_that_fails)
    monkeypatch.setattr(m.asyncio, "create_task", lambda coro: coro.close())

    asyncio.run(m._spawn_keep_warm_bots_and_start_reaper())  # must not raise


def test_shutdown_handler_stops_every_tracked_worker(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: True)
    monkeypatch.setattr(m.bot_processes, "_processes", {"coder": object(), "hydration-reminder": object()})
    stop_calls = []

    async def fake_stop(profile, **kwargs):
        stop_calls.append(profile)

    monkeypatch.setattr(m.bot_processes, "stop_bot_process", fake_stop)
    asyncio.run(m._stop_all_bot_processes())
    assert sorted(stop_calls) == ["coder", "hydration-reminder"]


def test_shutdown_handler_is_a_noop_under_embedded_transport(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: False)
    monkeypatch.setattr(m.bot_processes, "_processes", {"coder": object()})
    stop_calls = []
    monkeypatch.setattr(m.bot_processes, "stop_bot_process", AsyncMock(side_effect=lambda p, **k: stop_calls.append(p)))
    asyncio.run(m._stop_all_bot_processes())
    assert stop_calls == []


def test_bot_lifecycle_sweep_loop_reaps_using_roster_activity(monkeypatch):
    async def fake_get_roster(include_hidden=False):
        return [
            m.RosterEntry(name="default", title="Default", description="", last_active=1000.0),
            m.RosterEntry(name="coder", title="Coder", description="", last_active=1.0),
        ]

    reap_calls = []

    async def fake_reap(*, idle_since, keep_warm, threshold_s):
        reap_calls.append((idle_since, keep_warm, threshold_s))
        return []

    async def sleep_once_then_stop(_seconds):
        if len(reap_calls) >= 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_keep_warm_bots", AsyncMock(return_value={"default"}))
    monkeypatch.setattr(m.bot_processes, "reap_idle", fake_reap)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))
    monkeypatch.setattr(m.asyncio, "sleep", sleep_once_then_stop)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m._bot_lifecycle_sweep_loop())

    assert len(reap_calls) == 1
    idle_since, keep_warm, threshold_s = reap_calls[0]
    assert idle_since == {"default": 1000.0, "coder": 1.0}
    assert keep_warm == {"default"}
    assert threshold_s == m.IDLE_REAP_SECONDS


def test_bot_lifecycle_sweep_loop_re_ensures_keep_warm_bots(monkeypatch):
    # Real bug this guards against: a keep-warm bot that failed to boot
    # (or was manually stopped) needs a recurring retry, not just a
    # single startup-time attempt -- otherwise it stays down forever.
    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="default", title="Default", description="")]

    spawn_calls = []

    async def fake_wake(profile):
        spawn_calls.append(profile)
        return 8700

    async def sleep_once_then_stop(_seconds):
        if spawn_calls:
            raise asyncio.CancelledError()

    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_keep_warm_bots", AsyncMock(return_value={"default"}))
    monkeypatch.setattr(m.bot_processes, "reap_idle", AsyncMock(return_value=[]))
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", fake_wake)
    monkeypatch.setattr(m.asyncio, "sleep", sleep_once_then_stop)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(m._bot_lifecycle_sweep_loop())

    assert spawn_calls == ["default"]


# ---------------------------------------------------------------------------
# _get_roster_with_retry -- real bug found live: the FastAPI backend's own
# startup event can fire before the dashboard (a separate process started
# in its own background subshell) is actually accepting connections yet,
# so a single-attempt get_roster() at startup silently failed and
# "default" was never spawned under ZBOTS_CHAT_TRANSPORT=http.
# ---------------------------------------------------------------------------

def test_get_roster_with_retry_succeeds_on_a_later_attempt(monkeypatch):
    attempts = {"n": 0}

    async def flaky_get_roster(include_hidden=False):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise m.httpx.ConnectError("dashboard not up yet")
        return [m.RosterEntry(name="default", title="Default", description="")]

    monkeypatch.setattr(m, "get_roster", flaky_get_roster)
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock())

    roster = asyncio.run(m._get_roster_with_retry(attempts=5, delay_s=0))
    assert attempts["n"] == 3
    assert roster[0].name == "default"


def test_get_roster_with_retry_raises_after_exhausting_every_attempt(monkeypatch):
    async def always_fails(include_hidden=False):
        raise m.httpx.ConnectError("dashboard never came up")

    monkeypatch.setattr(m, "get_roster", always_fails)
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock())

    with pytest.raises(m.httpx.ConnectError):
        asyncio.run(m._get_roster_with_retry(attempts=3, delay_s=0))


def test_startup_handler_uses_the_retrying_roster_fetch(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: True)
    monkeypatch.setattr(m.asyncio, "create_task", lambda coro: coro.close())

    retry_calls = []

    async def fake_retry(**kwargs):
        retry_calls.append(kwargs)
        return [m.RosterEntry(name="default", title="Default", description="")]

    monkeypatch.setattr(m, "_get_roster_with_retry", fake_retry)
    monkeypatch.setattr(m, "_keep_warm_bots", AsyncMock(return_value={"default"}))
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))

    asyncio.run(m._spawn_keep_warm_bots_and_start_reaper())
    assert len(retry_calls) == 1


# ---------------------------------------------------------------------------
# Push notifications -- SendMessage.notify wiring (bot_send) and the
# /push/* endpoints. See push.py's own module docstring for why this
# exists and why it's real Web Push, not a same-tab Notification() call.
# ---------------------------------------------------------------------------

def test_bot_send_does_not_push_by_default(client, monkeypatch):
    monkeypatch.setattr(m, "send_to_bot", AsyncMock(return_value="a reply"))
    push_calls = []
    monkeypatch.setattr(m.push, "send_push_notification", AsyncMock(side_effect=lambda *a: push_calls.append(a)))

    resp = client.post("/bots/alpha/messages", json={"text": "hi"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "a reply"}
    assert push_calls == []


def test_bot_send_pushes_when_notify_is_set(client, monkeypatch):
    monkeypatch.setattr(m, "send_to_bot", AsyncMock(return_value="Hey Zainey, drink some water!"))
    push_calls = []
    monkeypatch.setattr(m.push, "send_push_notification", AsyncMock(side_effect=lambda *a: push_calls.append(a)))

    resp = client.post("/bots/hydration-reminder/messages", json={"text": "[internal-trigger] ...", "notify": True})
    assert resp.status_code == 200
    assert push_calls == [("Hydration Reminder", "Hey Zainey, drink some water!")]


def test_bot_send_does_not_push_an_empty_reply(client, monkeypatch):
    monkeypatch.setattr(m, "send_to_bot", AsyncMock(return_value=""))
    push_calls = []
    monkeypatch.setattr(m.push, "send_push_notification", AsyncMock(side_effect=lambda *a: push_calls.append(a)))

    resp = client.post("/bots/alpha/messages", json={"text": "hi", "notify": True})
    assert resp.status_code == 200
    assert push_calls == []


def test_get_vapid_public_key(client, monkeypatch):
    monkeypatch.setattr(m.push, "get_public_key_b64", lambda: "fake-public-key")
    resp = client.get("/push/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json() == {"key": "fake-public-key"}


def test_push_subscribe(client, monkeypatch):
    added = []
    monkeypatch.setattr(m.push, "add_subscription", lambda sub: added.append(sub))
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    resp = client.post("/push/subscribe", json={"subscription": sub})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert added == [sub]


def test_push_subscribe_rejects_a_subscription_with_no_endpoint(client, monkeypatch):
    def fake_add(sub):
        raise ValueError("subscription missing endpoint")

    monkeypatch.setattr(m.push, "add_subscription", fake_add)
    resp = client.post("/push/subscribe", json={"subscription": {"keys": {}}})
    assert resp.status_code == 400


def test_push_unsubscribe(client, monkeypatch):
    removed = []
    monkeypatch.setattr(m.push, "remove_subscription", lambda endpoint: removed.append(endpoint))
    resp = client.post("/push/unsubscribe", json={"endpoint": "https://push.example/abc"})
    assert resp.status_code == 200
    assert removed == ["https://push.example/abc"]
