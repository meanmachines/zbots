"""Shared test setup.

backend/main.py reads its configuration from the environment at import time,
so the env must be in place before the module is first imported anywhere in
the test session. The temp dirs created here are intentionally outside the
repo tree (tempfile.mkdtemp) so tests never touch real state.
"""

import os
import tempfile

_test_root = tempfile.mkdtemp(prefix="zbots-tests-")

os.environ["BOTS_UI_STATE_PATH"] = os.path.join(_test_root, "state.json")
os.environ["BOTS_UI_AVATAR_DIR"] = os.path.join(_test_root, "avatars")
os.environ["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = "testuser"
os.environ["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = "testpass"
os.environ["API_SERVER_KEY"] = "test-api-key"
os.environ["HERMES_DASHBOARD_URL"] = "http://hermes-test:9119"
os.environ["HERMES_API_SERVER_URL"] = "http://hermes-test:8642"
# Optional API-key auth is off by default; tests that exercise it set the
# module global directly so the rest of the suite stays unauthenticated.
os.environ.pop("BOTS_UI_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

import backend.main as m


@pytest.fixture()
def client():
    m._dash_logged_in = False
    m._read_state.cache_clear() if hasattr(m._read_state, "cache_clear") else None
    if m.STATE_PATH.exists():
        m.STATE_PATH.unlink()
    with TestClient(m.app) as c:
        yield c
    m.BOTS_UI_API_KEY = ""


@pytest.fixture()
def state_file():
    """Return the active state path so tests can seed/corrupt state."""
    return m.STATE_PATH
