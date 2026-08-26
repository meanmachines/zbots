"""Unit tests for backend/bot_processes.py -- the per-bot worker pool
manager (see its own module docstring, and bot_worker.py's, for why this
exists: replacing one shared multiplexed gateway process with a genuinely
separate OS process per bot). Every real subprocess/httpx call is
replaced with a fake, matching this session's own established testing
pattern (httpx.MockTransport for the Phase 1 http-transport tests) -- no
real process spawning or network I/O here.
"""

import asyncio
import json
import os

import httpx
import pytest

from backend import bot_processes


class _FakeProcess:
    """Stand-in for multiprocessing.Process -- controllable is_alive()
    instead of a real OS process, so tests can simulate "still starting",
    "running", "died on its own", and "ignored SIGTERM" without ever
    spawning anything real.
    """

    _next_pid = 90000

    def __init__(self, target=None, args=(), daemon=False):
        self.target = target
        self.args = args
        self._alive = False
        self._terminated = False
        self._killed = False
        self.pid = None
        # ignore_terminate: simulate a worker that doesn't exit on SIGTERM
        # (join() returns without the process actually dying) so
        # stop_bot_process's SIGKILL escalation path gets exercised.
        self.ignore_terminate = False

    def start(self):
        self._alive = True
        self.pid = _FakeProcess._next_pid
        _FakeProcess._next_pid += 1

    def is_alive(self):
        return self._alive

    def terminate(self):
        self._terminated = True
        if not self.ignore_terminate:
            self._alive = False

    def kill(self):
        self._killed = True
        self._alive = False

    def join(self, timeout=None):
        pass


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Every test gets its own registry file and a clean in-memory
    _processes dict -- state.json-style isolation, same reasoning as
    conftest.py's own tempfile-based BOTS_UI_STATE_PATH.
    """
    monkeypatch.setattr(bot_processes, "REGISTRY_PATH", tmp_path / "bot-processes.json")
    monkeypatch.setattr(bot_processes, "_processes", {})
    monkeypatch.setattr(bot_processes, "BASE_PORT", 8700)
    yield


def _fake_spawn_ctx(monkeypatch, process_cls=_FakeProcess):
    class _Ctx:
        def Process(self, target=None, args=(), daemon=False):
            return process_cls(target=target, args=args, daemon=daemon)

    monkeypatch.setattr(bot_processes, "_spawn_ctx", _Ctx())


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

def test_get_port_allocates_from_base_port():
    assert bot_processes.get_port("coder") == 8700


def test_get_port_is_stable_across_calls():
    first = bot_processes.get_port("coder")
    second = bot_processes.get_port("coder")
    assert first == second


def test_get_port_skips_already_allocated_ports():
    bot_processes.get_port("coder")  # 8700
    assert bot_processes.get_port("butler") == 8701


def test_get_port_persists_to_the_registry_file():
    bot_processes.get_port("coder")
    data = json.loads(bot_processes.REGISTRY_PATH.read_text())
    assert data["ports"]["coder"] == 8700


def test_get_port_survives_a_backend_restart():
    # Simulate a fresh backend process: nothing in memory, only the
    # on-disk registry from a prior process's allocation.
    bot_processes.REGISTRY_PATH.write_text(json.dumps({"ports": {"coder": 8712}, "workers": {}}))
    assert bot_processes.get_port("coder") == 8712
    assert bot_processes.get_port("new-bot") == 8700  # first free slot, not 8713


# ---------------------------------------------------------------------------
# Liveness -- _pid_alive / is_running
# ---------------------------------------------------------------------------

def test_pid_alive_true_for_this_process():
    assert bot_processes._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_a_pid_that_does_not_exist():
    # Real bug class this guards against: a recorded pid from a PRIOR
    # backend process is exactly this case after a restart.
    assert bot_processes._pid_alive(2**30 - 1) is False


def test_is_running_false_with_no_recorded_worker():
    assert bot_processes.is_running("coder") is False


def test_is_running_true_when_registry_records_a_live_pid():
    bot_processes.REGISTRY_PATH.write_text(
        json.dumps({"ports": {"coder": 8700}, "workers": {"coder": {"pid": os.getpid(), "port": 8700, "started_at": 0}}})
    )
    assert bot_processes.is_running("coder") is True


def test_is_running_false_when_registry_records_a_dead_pid():
    bot_processes.REGISTRY_PATH.write_text(
        json.dumps({"ports": {"coder": 8700}, "workers": {"coder": {"pid": 2**30 - 1, "port": 8700, "started_at": 0}}})
    )
    assert bot_processes.is_running("coder") is False


# ---------------------------------------------------------------------------
# ensure_bot_process_running -- spawn + readiness poll
# ---------------------------------------------------------------------------

def _use_health_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(bot_processes.httpx, "AsyncClient", _client)


def test_ensure_bot_process_running_spawns_and_waits_for_health(monkeypatch):
    _fake_spawn_ctx(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)

    port = asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    assert port == 8700
    assert calls["n"] == 1
    assert bot_processes.is_running("coder") is True


def test_ensure_bot_process_running_does_not_respawn_an_already_alive_worker(monkeypatch):
    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)

    asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    proc_after_first_call = bot_processes._processes["coder"]

    asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    assert bot_processes._processes["coder"] is proc_after_first_call


def test_ensure_bot_process_running_raises_on_a_health_timeout(monkeypatch):
    _fake_spawn_ctx(monkeypatch)
    monkeypatch.setattr(bot_processes, "READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot_processes, "READY_POLL_INTERVAL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("not listening yet", request=request)

    _use_health_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="did not become ready"):
        asyncio.run(bot_processes.ensure_bot_process_running("coder"))


def test_ensure_bot_process_running_self_heals_a_pid_reuse_false_positive(monkeypatch):
    _fake_spawn_ctx(monkeypatch)
    monkeypatch.setattr(bot_processes, "READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot_processes, "READY_POLL_INTERVAL_S", 0.01)

    # Real collision found live: the registry's recorded pid for "coder"
    # is a real, currently-alive process (this test process itself) that
    # just isn't actually coder's worker -- exactly what happens when a
    # dead worker's pid gets recycled by an unrelated later process in a
    # container with a small pid space. _is_worker_alive trusts it and
    # skips spawning, so the only thing that can catch this is the real
    # GET /health check below still failing against the port nothing is
    # actually listening on.
    bot_processes.REGISTRY_PATH.write_text(
        json.dumps(
            {
                "ports": {"coder": 8700},
                "workers": {"coder": {"pid": os.getpid(), "port": 8700, "started_at": 0}},
            }
        )
    )

    healed = {"done": False}
    real_forget = bot_processes._forget_worker

    def _tracking_forget(profile):
        real_forget(profile)
        healed["done"] = True

    monkeypatch.setattr(bot_processes, "_forget_worker", _tracking_forget)

    def handler(request: httpx.Request) -> httpx.Response:
        if not healed["done"]:
            raise httpx.ConnectError("nothing listening on the stale pid's claimed port", request=request)
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)

    port = asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    assert port == 8700
    assert healed["done"] is True
    assert bot_processes.is_running("coder") is True


def test_ensure_bot_process_running_raises_once_self_heal_also_fails(monkeypatch):
    _fake_spawn_ctx(monkeypatch)
    monkeypatch.setattr(bot_processes, "READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot_processes, "READY_POLL_INTERVAL_S", 0.01)

    bot_processes.REGISTRY_PATH.write_text(
        json.dumps(
            {
                "ports": {"coder": 8700},
                "workers": {"coder": {"pid": os.getpid(), "port": 8700, "started_at": 0}},
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("still nothing listening", request=request)

    _use_health_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="did not become ready"):
        asyncio.run(bot_processes.ensure_bot_process_running("coder"))


def test_ensure_bot_process_running_respawns_a_worker_that_died(monkeypatch):
    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)

    asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    bot_processes._processes["coder"]._alive = False  # simulate a crash

    asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    assert bot_processes._processes["coder"].is_alive() is True


# ---------------------------------------------------------------------------
# stop_bot_process -- graceful terminate, SIGKILL escalation
# ---------------------------------------------------------------------------

def test_stop_bot_process_terminates_a_cooperative_worker(monkeypatch):
    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)
    asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    proc = bot_processes._processes["coder"]

    asyncio.run(bot_processes.stop_bot_process("coder"))
    assert proc._terminated is True
    assert proc._killed is False
    assert "coder" not in bot_processes._processes
    assert bot_processes.is_running("coder") is False


def test_stop_bot_process_escalates_to_sigkill_for_an_uncooperative_worker(monkeypatch):
    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)
    asyncio.run(bot_processes.ensure_bot_process_running("coder"))
    proc = bot_processes._processes["coder"]
    proc.ignore_terminate = True

    asyncio.run(bot_processes.stop_bot_process("coder"))
    assert proc._terminated is True
    assert proc._killed is True


def test_stop_bot_process_is_a_noop_for_an_unknown_bot():
    asyncio.run(bot_processes.stop_bot_process("never-started"))  # must not raise


# ---------------------------------------------------------------------------
# reap_idle
# ---------------------------------------------------------------------------

def test_reap_idle_stops_an_idle_non_keep_warm_bot(monkeypatch):
    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)
    asyncio.run(bot_processes.ensure_bot_process_running("coder"))

    stopped = asyncio.run(
        bot_processes.reap_idle(idle_since={"coder": 0.0}, keep_warm=set(), threshold_s=1.0)
    )
    assert stopped == ["coder"]
    assert bot_processes.is_running("coder") is False


def test_reap_idle_never_stops_a_keep_warm_bot(monkeypatch):
    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)
    asyncio.run(bot_processes.ensure_bot_process_running("default"))

    stopped = asyncio.run(
        bot_processes.reap_idle(idle_since={"default": 0.0}, keep_warm={"default"}, threshold_s=1.0)
    )
    assert stopped == []
    assert bot_processes.is_running("default") is True


def test_reap_idle_leaves_a_recently_active_bot_running(monkeypatch):
    import time as _time

    _fake_spawn_ctx(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    _use_health_transport(monkeypatch, handler)
    asyncio.run(bot_processes.ensure_bot_process_running("coder"))

    stopped = asyncio.run(
        bot_processes.reap_idle(idle_since={"coder": _time.time()}, keep_warm=set(), threshold_s=600.0)
    )
    assert stopped == []
    assert bot_processes.is_running("coder") is True


def test_reap_idle_ignores_a_bot_with_no_recorded_activity():
    # No idle_since entry at all -- treated as "not eligible", not "idle
    # forever" (a bot that's never been dialed through this mechanism
    # yet, e.g. mid-rollout, shouldn't be reaped on the very first sweep).
    stopped = asyncio.run(
        bot_processes.reap_idle(idle_since={}, keep_warm=set(), threshold_s=1.0)
    )
    assert stopped == []
