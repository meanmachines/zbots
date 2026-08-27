"""Owns the pool of per-bot worker processes (see bot_worker.py's own
module docstring for why this exists -- removing the shared-process
scoping-bug class this session found twice, by giving every bot a
genuinely separate OS process instead of multiplexing one shared gateway).

Deliberately has NO dependency on main.py -- same one-directional pattern
engine.py already uses (main.py imports this module, never the reverse).
Port allocation and worker liveness/lifecycle live entirely in this
module's own small persisted registry (BOT_PROCESSES_STATE_PATH), separate
from main.py's own state.json. Bot-specific business logic this module
deliberately does NOT know about -- which bots have routines, what "recent
activity" means for the roster -- stays in main.py; reap_idle() below takes
that as plain parameters from the caller instead of reaching for it itself.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import psutil

try:
    from . import bot_worker
except ImportError:
    import bot_worker

REGISTRY_PATH = Path(os.environ.get("BOT_PROCESSES_STATE_PATH", "/opt/data/bot-processes.json"))
BASE_PORT = int(os.environ.get("BOT_PROCESS_BASE_PORT", "8700"))
READY_TIMEOUT_S = float(os.environ.get("BOT_PROCESS_READY_TIMEOUT_S", "30"))
READY_POLL_INTERVAL_S = 0.25

# In-memory Process handles -- only meaningful for workers THIS backend
# process spawned since its own last restart. A backend restart still
# correctly detects an already-running worker spawned by a PRIOR backend
# process via the registry's own recorded pid (see _is_worker_alive) --
# this dict is an optimization (skip the liveness syscall when we already
# hold the real handle), not the source of truth.
_processes: dict[str, "multiprocessing.Process"] = {}
_lock = asyncio.Lock()

_spawn_ctx = multiprocessing.get_context("spawn")


def _read_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"ports": {}, "workers": {}}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ports": {}, "workers": {}}
    if not isinstance(data, dict):
        return {"ports": {}, "workers": {}}
    data.setdefault("ports", {})
    data.setdefault("workers", {})
    return data


def _write_registry(data: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def get_port(profile: str) -> int:
    """This bot's assigned port, allocating one on first call. Persisted
    (not recomputed each time) so a backend restart doesn't reassign a bot
    to a different port a still-running worker from before the restart is
    actually listening on.
    """
    data = _read_registry()
    ports: dict[str, int] = data["ports"]
    if profile in ports:
        return ports[profile]
    used = set(ports.values())
    port = BASE_PORT
    while port in used:
        port += 1
    ports[profile] = port
    _write_registry(data)
    return port


def _pid_alive(pid: int) -> bool:
    """Real liveness check, not a cache read -- same methodology this
    session already used for session_turn_leases ("a lease held by a dead
    PID is orphaned, not real"), applied to worker ownership instead. A
    recorded pid from a PRIOR backend process (survived a backend restart)
    is exactly the case this exists for.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else -- shouldn't happen for our own
        # child, but "exists" is the honest answer either way.
        return True
    except OSError:
        return False
    return True


def _is_worker_alive(profile: str) -> bool:
    proc = _processes.get(profile)
    if proc is not None:
        return proc.is_alive()
    worker = _read_registry()["workers"].get(profile)
    if not worker:
        return False
    return _pid_alive(int(worker["pid"]))


async def ensure_bot_process_running(profile: str) -> int:
    """Returns this bot's port once its worker is confirmed ready (a real
    GET /health succeeded). Spawns the worker first if it wasn't already
    tracked as alive. Safe to call on every chat request -- the common
    case (already running) costs one liveness check and returns
    immediately without touching the network.

    Real bug found live: a dead worker's pid can be recycled by the OS for
    an unrelated later process (confirmed live -- two different profiles'
    registry entries pointing at the same pid, in a container with a small
    enough pid space for this to actually happen), which makes
    _is_worker_alive report a stale entry as alive and skip respawning it.
    This never misroutes a request (each profile still has its own port,
    so _wait_ready below is the real check, not the pid), but left
    uncorrected it would 30s-timeout-then-fail on *every* future call for
    that profile forever, since nothing ever cleared the bad registry
    entry. Self-heal: if the trusted-alive path's own _wait_ready still
    fails, treat that pid as a false positive, forget it, and spawn a real
    replacement before giving up.
    """
    port = get_port(profile)
    spawned = False
    async with _lock:
        if not _is_worker_alive(profile):
            _spawn(profile, port)
            spawned = True
    try:
        await _wait_ready(port)
    except RuntimeError:
        if spawned:
            raise
        async with _lock:
            _forget_worker(profile)
            _spawn(profile, port)
        await _wait_ready(port)
    return port


def _spawn(profile: str, port: int) -> None:
    proc = _spawn_ctx.Process(target=bot_worker.run_bot_worker, args=(profile, port), daemon=False)
    proc.start()
    _processes[profile] = proc
    data = _read_registry()
    data["workers"][profile] = {"pid": proc.pid, "port": port, "started_at": time.time()}
    _write_registry(data)


async def _wait_ready(port: int, timeout: float = READY_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last_exc = exc
            await asyncio.sleep(READY_POLL_INTERVAL_S)
    raise RuntimeError(
        f"bot worker on port {port} did not become ready within {timeout}s"
        + (f" (last error: {last_exc})" if last_exc else "")
    )


async def stop_bot_process(profile: str, *, grace_s: float = 10.0) -> None:
    """Best-effort clean shutdown -- SIGTERM (via Process.terminate()) and
    give start_gateway() its own graceful-shutdown path a real chance
    (confirmed live elsewhere in this session: the existing shared gateway
    process already shuts down cleanly on plain SIGTERM), SIGKILL only if
    it's still alive after the grace period.
    """
    proc = _processes.get(profile)
    if proc is None or not proc.is_alive():
        _processes.pop(profile, None)
        _forget_worker(profile)
        return
    proc.terminate()
    await asyncio.get_running_loop().run_in_executor(None, proc.join, grace_s)
    if proc.is_alive():
        proc.kill()
        await asyncio.get_running_loop().run_in_executor(None, proc.join, 5)
    _processes.pop(profile, None)
    _forget_worker(profile)


def _forget_worker(profile: str) -> None:
    data = _read_registry()
    if data["workers"].pop(profile, None) is not None:
        _write_registry(data)


def is_running(profile: str) -> bool:
    """Non-blocking liveness check for the roster/status surfaces --
    unlike ensure_bot_process_running, never spawns anything."""
    return _is_worker_alive(profile)


def listening_ports(profile: str) -> list[dict]:
    """Every TCP port a live dev server this bot's own tools started is
    currently listening on, found by walking the worker's own process tree.
    The vendored process/terminal tools have no concept of ports at all
    (confirmed live: no port field anywhere in process_registry.py's or
    terminal_tool.py's own events) -- this is the one reliable way to know
    "is there a live preview to show" without any bot behavior change or
    new tool. Safe only because a background process a bot starts shares
    this same container/process tree (see bot_worker.py's own module
    docstring) -- there's no sandbox boundary to cross.
    """
    worker = _read_registry()["workers"].get(profile)
    if not worker:
        return []
    pid = int(worker["pid"])
    if not _pid_alive(pid):
        return []
    try:
        root = psutil.Process(pid)
        candidate_pids = {pid} | {p.pid for p in root.children(recursive=True)}
    except psutil.NoSuchProcess:
        return []
    ports: dict[int, int] = {}
    for candidate_pid in candidate_pids:
        try:
            proc = psutil.Process(candidate_pid)
            # net_connections() is the current psutil name; connections()
            # is the same call under the name older psutil releases still
            # ship, kept as a fallback so this doesn't pin a version.
            try:
                conns = proc.net_connections(kind="inet")
            except AttributeError:
                conns = proc.connections(kind="inet")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for conn in conns:
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                ports[conn.laddr.port] = candidate_pid
    return [{"port": port, "pid": owner_pid} for port, owner_pid in sorted(ports.items())]


async def reap_idle(*, idle_since: dict[str, float], keep_warm: set[str], threshold_s: float) -> list[str]:
    """Stop every currently-running, non-keep-warm worker whose bot has
    been idle past threshold_s. idle_since maps profile -> last-activity
    unix timestamp, keep_warm is the set of profiles that should never be
    reaped regardless of activity (routine-bearing bots -- see the
    project plan's "Lifecycle" section for why keep-warm classification is
    the caller's job, not this module's). Returns the profiles actually
    stopped.
    """
    now = time.time()
    stopped = []
    data = _read_registry()
    for profile in list(data["workers"].keys()):
        if profile in keep_warm:
            continue
        last_active = idle_since.get(profile)
        if last_active is None or (now - last_active) < threshold_s:
            continue
        if not _is_worker_alive(profile):
            _forget_worker(profile)
            continue
        await stop_bot_process(profile)
        stopped.append(profile)
    return stopped
