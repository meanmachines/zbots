"""Entry point for one bot's dedicated OS process -- a genuinely isolated,
single-profile ``hermes gateway run``, owned and spawned directly by zBots'
own `bot_processes.py` via Python's `multiprocessing`, not by shelling out
to the `hermes` CLI as an external tool.

Why this exists: this session found two real credential/config-scoping bugs
(see CHANGELOG's "gateway.multiplex_profiles"/"provider definitions"/
"provider credentials" entries) in the previous architecture -- one shared
`hermes gateway run` process, multiplexing every bot's chat traffic through
per-request profile scoping (`/p/<profile>/` URL prefixes,
gateway.multiplex_profiles). Both were fixed, but they're two instances of
the same underlying risk class: request-time scoping inside one shared
process is an ongoing surface for this exact kind of bug -- and `default`
(the one profile that's never had a scoping bug, because its own config
already IS the unscoped root config) is the existence proof that removing
the sharing removes the bug class entirely, not just the two instances
found so far.

This module IS that removal: each bot gets a real, separate process
running `gateway.run.start_gateway()` -- the exact same top-level "run
until interrupted" entry point `hermes gateway run`'s own CLI command
calls, unmodified, un-wrapped. Confirmed by reading `GatewayRunner.start()`
directly (gateway/run.py, iterates `config.platforms.items()` and
constructs+connects each configured adapter itself, including
APIServerAdapter for Platform.API_SERVER): no manual adapter construction
is needed here at all, unlike engine.py's `_build_runner_and_adapter()`
(which deliberately skips `runner.start()`/`adapter.connect()` for the
embedded/mocked-request transport -- that function is NOT used by this
module, and stays exactly as it is for that other purpose).

Process isolation is what makes this correct without any additional
scoping logic: `HERMES_HOME` is repointed to this ONE profile's own
directory before any hermes-agent import happens, so every config/secret
read for the whole lifetime of this process resolves against that
profile's own files -- the same plain, un-scoped code path `default` (and,
before it, every single-profile Hermes install ever) already uses
correctly. No contextvar, no URL-prefix middleware, nothing to get out of
sync with a sibling request.

PID-file safety: `start_gateway()`'s own duplicate-instance guard is
scoped to HERMES_HOME (confirmed in its own docstring: "future
multi-profile setups (each profile using a distinct HERMES_HOME) will
naturally allow concurrent instances without tripping this guard") -- so
running many of these concurrently, one per profile directory, is exactly
the scenario that guard was already built to allow.
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_profile_home(profile: str) -> Path:
    """Same resolution main.py's own `_provision_profile_api_server_key`/
    `_sync_profile_provider` already rely on (root HERMES_HOME for
    "default", root/profiles/<name> otherwise) -- deliberately plain
    pathlib, no vendor import, so this can run BEFORE HERMES_HOME is
    repointed (see run_bot_worker's own ordering comment).
    """
    root = Path(os.environ["HERMES_HOME"])
    if profile == "default":
        return root
    return root / "profiles" / profile


def run_bot_worker(profile: str, port: int) -> None:
    """Process entry point -- call this as the `target` of a
    `multiprocessing.Process`, never call it directly in the parent
    process (it mutates process-wide env vars and blocks forever).

    Ordering matters: `_resolve_profile_home` reads the INHERITED
    HERMES_HOME (the container root -- multiprocessing's "spawn" start
    method inherits the parent's os.environ at the moment `.start()` is
    called) to find this profile's own directory, THEN os.environ is
    overwritten to that directory before any hermes-agent module is
    imported -- hermes_constants.get_hermes_home() and everything built on
    it (load_config, get_secret, the gateway's own PID-file path, ...)
    reads this at call time, and several modules resolve profile-relative
    paths at import time too, so setting it late risks a partially-scoped
    process.
    """
    import asyncio
    import sys

    profile_home = _resolve_profile_home(profile)
    os.environ["HERMES_HOME"] = str(profile_home)
    os.environ["API_SERVER_PORT"] = str(port)

    vendor_root = Path(__file__).resolve().parent.parent / "vendor" / "hermes-agent"
    if str(vendor_root) not in sys.path:
        sys.path.insert(0, str(vendor_root))

    from gateway.run import start_gateway

    asyncio.run(start_gateway())
