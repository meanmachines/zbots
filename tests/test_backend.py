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
        # /api/profiles is the profile-create call this test cares about;
        # create_bot also unconditionally PUTs a branded soul afterward
        # (see persona.with_branding_safety's own real-bug comment in
        # main.py) -- only capture the one this test is actually about.
        if path == "/api/profiles":
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


# ---------------------------------------------------------------------------
# Real bug found live: create_bot used to only PUT a soul when the caller
# explicitly supplied one (`if body.soul: ...`), so a bot created with no
# soul field (the common case) never got persona.py's branded default at
# all -- it fell straight back to hermes-agent's own raw stock persona ("You
# are Hermes Agent... created by Nous Research"). Confirmed live: 6 of 9
# real bots on zbots-dev had exactly this, including two created earlier the
# same session with no soul set. persona.with_branding_safety(body.soul) is
# now called unconditionally.
# ---------------------------------------------------------------------------

def test_create_bot_applies_the_branded_default_soul_when_none_given(client, monkeypatch):
    soul_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        if path == "/api/profiles/alpha/soul":
            soul_calls.append(body["content"])
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_infer_bot_category", AsyncMock(return_value="general"))

    resp = client.post("/bots", json={"name": "alpha", "title": "Alpha", "description": "d"})
    assert resp.status_code == 200
    assert soul_calls == [m.persona.DEFAULT_SOUL]


def test_create_bot_still_adds_branding_safety_to_a_custom_soul(client, monkeypatch):
    soul_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        if path == "/api/profiles/alpha/soul":
            soul_calls.append(body["content"])
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m, "_infer_bot_category", AsyncMock(return_value="general"))

    resp = client.post(
        "/bots",
        json={"name": "alpha", "title": "Alpha", "description": "d", "soul": "You are a research specialist."},
    )
    assert resp.status_code == 200
    assert soul_calls[0].startswith("You are a research specialist.")
    assert m.persona.BRANDING_SAFETY in soul_calls[0]


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
    monkeypatch.setattr(m, "_infer_bot_category", AsyncMock(return_value="general"))

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
# task-category bots -- deliberately stateless. Real design point: a task
# bot has no business remembering a previous unrelated ask, so every call
# ignores whatever session id is on record and always starts fresh.
#
# Real bug found live (see engine.py's _ensure_bot_chat_session /
# force_new_session docstrings): passing active_session_id=None alone does
# NOT get a fresh session once one already exists -- a second message to a
# live task bot landed back on the FIRST message's own session, and a
# "secret" told to it in message 1 came right back in message 2's reply.
# force_new_session=True is the actual fix; these tests assert BOTH that
# active_session_id is ignored (still None) AND that force_new_session is
# the True/False signal doing the real work.
# ---------------------------------------------------------------------------

def test_send_to_bot_ignores_the_stored_session_for_a_task_bot(monkeypatch, state_file):
    m._write_state({**m._default_state(), "active_sessions": {"quick-answers": "old-sid"}, "categories": {"quick-answers": "task"}})
    fake_engine_send = AsyncMock(return_value=("a fresh answer", "new-sid"))
    monkeypatch.setattr(m._engine, "send_to_bot", fake_engine_send)

    asyncio.run(m.send_to_bot("quick-answers", "hello"))

    call_args = fake_engine_send.await_args.args
    call_kwargs = fake_engine_send.await_args.kwargs
    assert call_args[3] is None  # active_session_id passed as None despite the stored "old-sid"
    assert call_kwargs["force_new_session"] is True


def test_send_to_bot_still_uses_the_stored_session_for_a_non_task_bot(monkeypatch, state_file):
    m._write_state({**m._default_state(), "active_sessions": {"butler": "old-sid"}, "categories": {"butler": "general"}})
    fake_engine_send = AsyncMock(return_value=("continuing", "old-sid"))
    monkeypatch.setattr(m._engine, "send_to_bot", fake_engine_send)

    asyncio.run(m.send_to_bot("butler", "hello"))

    call_args = fake_engine_send.await_args.args
    call_kwargs = fake_engine_send.await_args.kwargs
    assert call_args[3] == "old-sid"
    assert call_kwargs["force_new_session"] is False


def test_stream_to_bot_ignores_the_stored_session_for_a_task_bot(monkeypatch, state_file):
    m._write_state({**m._default_state(), "active_sessions": {"quick-answers": "old-sid"}, "categories": {"quick-answers": "task"}})
    seen = {}

    async def fake_engine_stream(profile, message, api_key, active_session_id, force_new_session=False):
        seen["active_session_id"] = active_session_id
        seen["force_new_session"] = force_new_session
        return {"session_id": "new-sid"}, _empty_async_iter()

    monkeypatch.setattr(m._engine, "stream_to_bot", fake_engine_stream)

    async def drain():
        async for _chunk in m.stream_to_bot("quick-answers", "hello"):
            pass

    asyncio.run(drain())
    assert seen == {"active_session_id": None, "force_new_session": True}


async def _empty_async_iter():
    return
    yield  # pragma: no cover -- makes this an async generator


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


def test_keep_warm_bots_includes_a_developer_category_bot(monkeypatch, state_file):
    state_file.write_text(json.dumps({"categories": {"coder": "developer"}}))

    async def fake_dash_get(path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "coder", "botty"]))
    assert warm == {"default", "coder"}


def test_keep_warm_bots_includes_a_chore_category_bot_with_no_routine_yet(monkeypatch, state_file):
    # A brand-new chore-category bot should be warm from the start, not
    # only once its own routine happens to exist and be enabled.
    state_file.write_text(json.dumps({"categories": {"hydration-reminder": "chore"}}))

    async def fake_dash_get(path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "hydration-reminder"]))
    assert warm == {"default", "hydration-reminder"}


def test_keep_warm_bots_includes_a_supervisor_category_bot(monkeypatch, state_file):
    state_file.write_text(json.dumps({"categories": {"qc-bot": "supervisor"}}))

    async def fake_dash_get(path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "qc-bot"]))
    assert warm == {"default", "qc-bot"}


def test_keep_warm_bots_does_not_include_a_task_category_bot(monkeypatch, state_file):
    state_file.write_text(json.dumps({"categories": {"quick-answers": "task"}}))

    async def fake_dash_get(path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "quick-answers"]))
    assert warm == {"default"}


def test_keep_warm_bots_does_not_include_a_general_category_bot_without_a_routine(monkeypatch, state_file):
    state_file.write_text(json.dumps({"categories": {"butler": "general"}}))

    async def fake_dash_get(path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(m, "dash_get", fake_dash_get)
    warm = asyncio.run(m._keep_warm_bots(["default", "butler"]))
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
# GET /bots/{name}/messages -- ensure_bot_process_running wiring. Real bug
# found live: this read path had no such call at all, unlike send/stream,
# and silently depended on bot_wake's own fire-and-forget pre-warm (which
# swallows its own errors) having already run -- a worker that died since
# its last chat (idle-reaped, crashed, or never started) 500'd with a raw
# connection error instead of transparently respawning, same as every
# other bot-facing route already does.
# ---------------------------------------------------------------------------

def test_bot_messages_ensures_the_worker_is_running_under_http(client, monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: True)
    wake_calls = []
    monkeypatch.setattr(
        m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=lambda p: wake_calls.append(p))
    )
    monkeypatch.setattr(m._engine, "get_bot_messages", AsyncMock(return_value=[]))

    resp = client.get("/bots/coder/messages")
    assert resp.status_code == 200
    assert wake_calls == ["coder"]


def test_bot_messages_skips_ensure_under_embedded_transport(client, monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: False)
    wake_calls = []
    monkeypatch.setattr(
        m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=lambda p: wake_calls.append(p))
    )
    monkeypatch.setattr(m._engine, "get_bot_messages", AsyncMock(return_value=[]))

    resp = client.get("/bots/coder/messages")
    assert resp.status_code == 200
    assert wake_calls == []


def test_bot_messages_surfaces_a_worker_that_never_becomes_ready(monkeypatch):
    monkeypatch.setattr(m._engine, "_use_http", lambda: True)
    monkeypatch.setattr(
        m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=RuntimeError("did not become ready"))
    )

    with pytest.raises(RuntimeError, match="did not become ready"):
        asyncio.run(m.get_bot_messages("coder"))


# ---------------------------------------------------------------------------
# Workspace panel -- _touched_paths_from_messages / GET /bots/{name}/workspace/file.
# Real gap found live: the existing /files page is locked to /opt/data, but a
# bot's own tools can (and did, live) write well outside that (/root/kvstore/).
# This is a differently-scoped, per-bot surface -- see main.py's own comment
# on _touched_paths_from_messages for why arguments (not each tool's own
# differently-shaped result) is the one reliable source of "what path did
# this call touch" across both write_file and read_file.
# ---------------------------------------------------------------------------

def _tool_call_message(name, arguments):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


def test_touched_paths_from_messages_extracts_write_and_read_file_paths():
    messages = [
        _tool_call_message("write_file", {"path": "/root/kvstore/server.py", "content": "x"}),
        _tool_call_message("read_file", {"path": "/root/kvstore/README.md"}),
    ]
    assert m._touched_paths_from_messages(messages) == {"/root/kvstore/server.py", "/root/kvstore/README.md"}


def test_touched_paths_from_messages_ignores_non_file_tool_calls():
    messages = [_tool_call_message("terminal", {"command": "ls /root/kvstore"})]
    assert m._touched_paths_from_messages(messages) == set()


def test_touched_paths_from_messages_ignores_messages_with_no_tool_calls():
    assert m._touched_paths_from_messages([{"role": "user", "content": "hi"}]) == set()


def test_touched_paths_from_messages_survives_malformed_arguments_json():
    messages = [{"role": "assistant", "tool_calls": [{"function": {"name": "write_file", "arguments": "{not json"}}]}]
    assert m._touched_paths_from_messages(messages) == set()  # must not raise


def test_get_workspace_file_serves_a_path_the_bot_has_touched(client, monkeypatch, tmp_path):
    real_file = tmp_path / "server.py"
    real_file.write_text("print('hi')", encoding="utf-8")
    messages = [_tool_call_message("write_file", {"path": str(real_file), "content": "print('hi')"})]
    monkeypatch.setattr(m, "get_bot_messages", AsyncMock(return_value=messages))

    resp = client.get("/bots/coder/workspace/file", params={"path": str(real_file)})
    assert resp.status_code == 200
    assert resp.json() == {"path": str(real_file), "content": "print('hi')"}


def test_get_workspace_file_rejects_a_path_the_bot_never_touched(client, monkeypatch, tmp_path):
    untouched = tmp_path / "secret.env"
    untouched.write_text("SECRET=1", encoding="utf-8")
    monkeypatch.setattr(m, "get_bot_messages", AsyncMock(return_value=[]))

    resp = client.get("/bots/coder/workspace/file", params={"path": str(untouched)})
    assert resp.status_code == 404


def test_get_workspace_file_404s_on_a_touched_path_that_no_longer_exists(client, monkeypatch, tmp_path):
    gone = tmp_path / "deleted.py"
    messages = [_tool_call_message("write_file", {"path": str(gone), "content": "x"})]
    monkeypatch.setattr(m, "get_bot_messages", AsyncMock(return_value=messages))

    resp = client.get("/bots/coder/workspace/file", params={"path": str(gone)})
    assert resp.status_code == 404


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


# ---------------------------------------------------------------------------
# get_bot_activity -- real bug found live, right after the rollover
# context-bridging fix shipped: a bridged retry becomes the fresh
# session's own opening message, and the native `preview` field (set from
# a session's first stored message) showed the internal recap note in the
# roster instead of the user's actual last message.
# ---------------------------------------------------------------------------

def test_get_bot_activity_strips_a_context_bridge_note(monkeypatch):
    async def fake_list_sessions(profile, headers):
        return [{"id": "sid-1", "preview": "[A brief technical hiccup...]\n\nit should remind every 5 minute", "started_at": 1.0}]

    monkeypatch.setattr(m._engine, "_list_bot_sessions", fake_list_sessions)
    monkeypatch.setattr(
        m._engine, "strip_context_bridge_note", lambda text: text.split("\n\n", 1)[-1] if "hiccup" in text else text
    )

    activity = asyncio.run(m.get_bot_activity("default"))
    assert activity["preview"] == "it should remind every 5 minute"


def test_get_bot_activity_leaves_a_normal_preview_untouched(monkeypatch):
    async def fake_list_sessions(profile, headers):
        return [{"id": "sid-1", "preview": "just a normal message", "started_at": 1.0}]

    monkeypatch.setattr(m._engine, "_list_bot_sessions", fake_list_sessions)
    activity = asyncio.run(m.get_bot_activity("default"))
    assert activity["preview"] == "just a normal message"


# ---------------------------------------------------------------------------
# _infer_bot_category / _validate_category -- bot lifecycle categories,
# requested live: "it should be inferred [from the description]... and the
# user can manually change its type as well later if required." See
# _keep_warm_bots() for what each category actually changes.
# ---------------------------------------------------------------------------

def test_validate_category_accepts_every_known_value():
    for c in m.CATEGORIES:
        assert m._validate_category(c) == c


def test_validate_category_is_case_insensitive():
    assert m._validate_category("CHORE") == "chore"


def test_validate_category_rejects_an_unknown_value():
    with pytest.raises(m.HTTPException):
        m._validate_category("not-a-real-category")


def test_infer_bot_category_skips_the_llm_call_for_an_empty_description(monkeypatch):
    wake_calls = []
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=lambda p: wake_calls.append(p)))
    category = asyncio.run(m._infer_bot_category(""))
    assert category == "general"
    assert wake_calls == []


def test_infer_bot_category_parses_a_valid_reply(monkeypatch):
    # http transport, not embedded -- avoids _profile_scope's embedded
    # branch constructing the real vendored adapter (needs packages not
    # installed in this test environment); everything this test actually
    # exercises is mocked regardless of transport.
    monkeypatch.setattr(m._engine, "_CHAT_TRANSPORT", "http")

    async def fake_create_session(profile, title, headers):
        assert profile == "default"
        return "temp-sid"

    async def fake_call_handler(handler_name, *, profile, method, path, json_body=None, headers=None, match_info=None, query=None):
        assert "chore" in json_body["message"]  # the category menu made it into the prompt
        return 200, {"message": {"content": "chore"}}

    delete_calls = []

    async def fake_delete(*args, **kwargs):
        delete_calls.append(kwargs.get("headers"))
        return _fake_response(200, {})

    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))
    monkeypatch.setattr(m._engine, "_create_bot_session", fake_create_session)
    monkeypatch.setattr(m._engine, "_call_handler", fake_call_handler)
    monkeypatch.setattr(m.httpx.AsyncClient, "delete", fake_delete)

    category = asyncio.run(m._infer_bot_category("reminds me to drink water every hour"))
    assert category == "chore"
    assert len(delete_calls) == 1  # temp session was cleaned up


def test_infer_bot_category_falls_back_on_an_unparseable_reply(monkeypatch):
    monkeypatch.setattr(m._engine, "_CHAT_TRANSPORT", "http")

    async def fake_create_session(profile, title, headers):
        return "temp-sid"

    async def fake_call_handler(handler_name, **kwargs):
        return 200, {"message": {"content": "I'm not sure, maybe a general assistant?"}}

    async def fake_delete(*args, **kwargs):
        return _fake_response(200, {})

    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))
    monkeypatch.setattr(m._engine, "_create_bot_session", fake_create_session)
    monkeypatch.setattr(m._engine, "_call_handler", fake_call_handler)
    monkeypatch.setattr(m.httpx.AsyncClient, "delete", fake_delete)

    category = asyncio.run(m._infer_bot_category("something ambiguous"))
    assert category == "general"


def test_infer_bot_category_falls_back_on_any_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("worker unreachable")

    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", boom)
    category = asyncio.run(m._infer_bot_category("a description"))  # must not raise
    assert category == "general"


def test_infer_bot_category_cleans_up_even_when_the_chat_call_fails(monkeypatch):
    monkeypatch.setattr(m._engine, "_CHAT_TRANSPORT", "http")

    async def fake_create_session(profile, title, headers):
        return "temp-sid"

    async def boom(*args, **kwargs):
        raise RuntimeError("chat call failed")

    delete_calls = []

    async def fake_delete(*args, **kwargs):
        delete_calls.append(1)
        return _fake_response(200, {})

    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))
    monkeypatch.setattr(m._engine, "_create_bot_session", fake_create_session)
    monkeypatch.setattr(m._engine, "_call_handler", boom)
    monkeypatch.setattr(m.httpx.AsyncClient, "delete", fake_delete)

    category = asyncio.run(m._infer_bot_category("a description"))
    assert category == "general"
    assert delete_calls == [1]  # cleanup still ran despite the chat call failing


def test_create_bot_uses_an_explicit_category_without_inferring(client, monkeypatch):
    infer_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="", provider="prov-a", model="model-a")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))
    monkeypatch.setattr(m, "_infer_bot_category", AsyncMock(side_effect=lambda d: infer_calls.append(d)))

    resp = client.post("/bots", json={"name": "alpha", "description": "d", "category": "developer"})
    assert resp.status_code == 200
    assert infer_calls == []  # explicit category skips inference entirely

    state = m._read_state()
    assert state["categories"]["alpha"] == "developer"


def test_create_bot_infers_when_no_explicit_category_given(client, monkeypatch):
    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="", provider="prov-a", model="model-a")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))
    monkeypatch.setattr(m, "_infer_bot_category", AsyncMock(return_value="task"))

    resp = client.post("/bots", json={"name": "alpha", "description": "answers one-off questions"})
    assert resp.status_code == 200
    state = m._read_state()
    assert state["categories"]["alpha"] == "task"


def test_update_bot_sets_an_explicit_category_override(client, monkeypatch):
    async def fake_dash_send(method, path, body=None, query=None):
        return {}

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    resp = client.patch("/bots/alpha", json={"category": "supervisor"})
    assert resp.status_code == 200
    state = m._read_state()
    assert state["categories"]["alpha"] == "supervisor"


def test_create_bot_tunes_the_profile_when_category_is_developer(client, monkeypatch):
    tuning_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        if path == "/api/config":
            tuning_calls.append((body, query))
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))

    resp = client.post("/bots", json={"name": "alpha", "description": "d", "category": "developer"})
    assert resp.status_code == 200
    assert len(tuning_calls) == 1
    body, query = tuning_calls[0]
    assert query == {"profile": "alpha"}
    assert body["config"] == m._DEVELOPER_PROFILE_TUNING


def test_create_bot_does_not_tune_the_profile_for_a_non_developer_category(client, monkeypatch):
    tuning_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        if path == "/api/config":
            tuning_calls.append((body, query))
        return {}

    async def fake_default_model():
        return "default-prov", "default-model"

    async def fake_get_roster(include_hidden=False):
        return [m.RosterEntry(name="alpha", title="Alpha", description="")]

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    monkeypatch.setattr(m, "_default_model", fake_default_model)
    monkeypatch.setattr(m, "get_roster", fake_get_roster)
    monkeypatch.setattr(m.bot_processes, "ensure_bot_process_running", AsyncMock(return_value=8700))

    resp = client.post("/bots", json={"name": "alpha", "description": "d", "category": "general"})
    assert resp.status_code == 200
    assert tuning_calls == []


def test_update_bot_tunes_the_profile_when_switched_to_developer(client, monkeypatch):
    tuning_calls = []

    async def fake_dash_send(method, path, body=None, query=None):
        if path == "/api/config":
            tuning_calls.append((body, query))
        return {}

    monkeypatch.setattr(m, "dash_send", fake_dash_send)
    resp = client.patch("/bots/coder", json={"category": "developer"})
    assert resp.status_code == 200
    assert len(tuning_calls) == 1
    body, query = tuning_calls[0]
    assert query == {"profile": "coder"}
    assert body["config"] == m._DEVELOPER_PROFILE_TUNING


def test_tune_developer_profile_is_best_effort(monkeypatch):
    monkeypatch.setattr(m, "dash_send", AsyncMock(side_effect=RuntimeError("boom")))
    asyncio.run(m._tune_developer_profile("coder"))  # must not raise


def test_update_bot_rejects_an_invalid_category(client):
    resp = client.patch("/bots/alpha", json={"category": "not-a-real-category"})
    assert resp.status_code == 400


def test_roster_reflects_a_bots_category(client, monkeypatch, state_file):
    state_file.write_text(json.dumps({"categories": {"alpha": "developer"}}))
    monkeypatch.setattr(m, "dash_get", AsyncMock(return_value={"profiles": [_profile("alpha")]}))
    monkeypatch.setattr(m, "get_bot_activity", AsyncMock(return_value={}))
    rows = client.get("/roster").json()
    assert rows[0]["category"] == "developer"


def test_roster_defaults_a_bot_with_no_category_to_general(client, monkeypatch):
    monkeypatch.setattr(m, "dash_get", AsyncMock(return_value={"profiles": [_profile("alpha")]}))
    monkeypatch.setattr(m, "get_bot_activity", AsyncMock(return_value={}))
    rows = client.get("/roster").json()
    assert rows[0]["category"] == "general"


# ---------------------------------------------------------------------------
# POST /bots/{name}/wake -- opportunistic pre-warm fired when a bot's chat
# is opened in the UI, before any message is actually sent.
# ---------------------------------------------------------------------------

def test_wake_ensures_the_bots_worker_is_running(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=lambda p: calls.append(p))
    )
    resp = client.post("/bots/alpha/wake")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls == ["alpha"]


def test_wake_never_fails_the_request_when_the_worker_fails_to_start(client, monkeypatch):
    monkeypatch.setattr(
        m.bot_processes, "ensure_bot_process_running", AsyncMock(side_effect=RuntimeError("boom"))
    )
    resp = client.post("/bots/alpha/wake")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Branding scrub on hermes-agent's own native dashboard-API proxies.
# Real bug found live: /connectors, /skills, /mcp/catalog, /plugins are all
# thin dash_get passthroughs (same convention as everywhere else in this
# file), and hermes-agent's own response text for these carries its own
# branding verbatim -- 27 of 33 connector platforms alone, confirmed live
# against the deployed app. persona.scrub_branding_deep is the fix; these
# confirm it's actually wired into each of the four endpoints, not just
# that the helper itself works (already covered in test_persona.py).
# ---------------------------------------------------------------------------

def test_connectors_endpoint_scrubs_platform_descriptions(client, monkeypatch):
    monkeypatch.setattr(
        m,
        "dash_get",
        AsyncMock(return_value={"platforms": [{"name": "Discord", "description": "Connect Hermes to Discord."}]}),
    )
    body = client.get("/connectors").json()
    assert "Hermes" not in body["platforms"][0]["description"]


def test_skills_endpoint_scrubs_skill_descriptions(client, monkeypatch):
    monkeypatch.setattr(
        m, "dash_get", AsyncMock(return_value=[{"name": "hermes-agent", "description": "Use Hermes Agent."}])
    )
    body = client.get("/skills").json()
    assert "Hermes" not in body[0]["description"]


def test_mcp_catalog_endpoint_scrubs_integration_help_text(client, monkeypatch):
    monkeypatch.setattr(
        m, "dash_get", AsyncMock(return_value={"integrations": [{"help": "On first connection Hermes opens..."}]})
    )
    body = client.get("/mcp/catalog").json()
    assert "Hermes" not in body["integrations"][0]["help"]


def test_plugins_endpoint_scrubs_plugin_names_and_descriptions(client, monkeypatch):
    monkeypatch.setattr(
        m,
        "dash_get",
        AsyncMock(return_value=[{"name": "hermes-achievements", "description": "Vibe coding and agentic Hermes workflows."}]),
    )
    body = client.get("/plugins").json()
    assert body[0]["name"] == "zBots-achievements"
    assert "Hermes" not in body[0]["description"]


def test_mcp_catalog_endpoint_leaves_docs_url_untouched(client, monkeypatch):
    monkeypatch.setattr(
        m,
        "dash_get",
        AsyncMock(return_value={"integrations": [{"docs_url": "https://hermes-agent.nousresearch.com/docs/x"}]}),
    )
    body = client.get("/mcp/catalog").json()
    assert body["integrations"][0]["docs_url"] == "https://hermes-agent.nousresearch.com/docs/x"


# ---------------------------------------------------------------------------
# POST /bots/{name}/steer -- redirect a bot while it's still actively
# working, instead of queuing a new turn for after it finishes. Real,
# native hermes-agent capability (POST /v1/runs/{run_id}/steer), found by
# reading a real steering incident live off the user's own hermes-agent
# desktop app mid-build.
# ---------------------------------------------------------------------------

def test_steer_uses_the_live_run_id_when_a_stream_is_in_flight(client, monkeypatch):
    m._active_streams["alpha"] = {"run_id": "run_abc"}
    steer_calls = []

    async def fake_steer_run(profile, run_id, text, api_key):
        steer_calls.append((profile, run_id, text))
        return {"accepted": True}

    monkeypatch.setattr(m._engine, "steer_run", fake_steer_run)
    try:
        resp = client.post("/bots/alpha/steer", json={"text": "keep going"})
    finally:
        m._active_streams.pop("alpha", None)

    assert resp.status_code == 200
    assert resp.json() == {"steered": True}
    assert steer_calls == [("alpha", "run_abc", "keep going")]


def test_steer_falls_back_to_a_normal_message_with_no_live_run(client, monkeypatch):
    m._active_streams.pop("alpha", None)  # confirm test isolation -- nothing left over
    monkeypatch.setattr(m, "send_to_bot", AsyncMock(return_value="a reply"))

    resp = client.post("/bots/alpha/steer", json={"text": "hello"})

    assert resp.status_code == 200
    assert resp.json() == {"steered": False, "reply": "a reply"}


def test_steer_falls_back_to_a_normal_message_when_the_real_api_rejects_it(client, monkeypatch):
    # Real, expected case, not an error: the run finished in the gap
    # between the client seeing it as "still active" and the steer
    # actually landing.
    m._active_streams["alpha"] = {"run_id": "run_abc"}
    monkeypatch.setattr(m._engine, "steer_run", AsyncMock(return_value={"accepted": False}))
    monkeypatch.setattr(m, "send_to_bot", AsyncMock(return_value="a fallback reply"))
    try:
        resp = client.post("/bots/alpha/steer", json={"text": "keep going"})
    finally:
        m._active_streams.pop("alpha", None)

    assert resp.status_code == 200
    assert resp.json() == {"steered": False, "reply": "a fallback reply"}


def test_steer_falls_back_when_steer_run_itself_raises(client, monkeypatch):
    m._active_streams["alpha"] = {"run_id": "run_abc"}
    monkeypatch.setattr(m._engine, "steer_run", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(m, "send_to_bot", AsyncMock(return_value="a fallback reply"))
    try:
        resp = client.post("/bots/alpha/steer", json={"text": "keep going"})
    finally:
        m._active_streams.pop("alpha", None)

    assert resp.status_code == 200
    assert resp.json() == {"steered": False, "reply": "a fallback reply"}


def test_steer_requires_non_empty_text(client):
    resp = client.post("/bots/alpha/steer", json={"text": "   "})
    assert resp.status_code == 400
