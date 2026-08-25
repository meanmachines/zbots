"""Unit tests for backend/push.py -- real Web Push notifications for
routine/delegated-task deliveries (see its own module docstring for why:
requested live, "browser push notification... works even if the zBots
tab isn't focused" -- nothing in hermes-agent itself was reusable, this
is a new capability). Real pywebpush/py_vapid calls are exercised for key
generation/persistence (cheap, local, no network); the actual network
send (`webpush()`) is mocked -- never a real push service call in tests.
"""

import asyncio
import json

import pytest

from backend import push


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "VAPID_KEY_PATH", tmp_path / "vapid-private.pem")
    monkeypatch.setattr(push, "SUBSCRIPTIONS_PATH", tmp_path / "subscriptions.json")
    monkeypatch.setattr(push, "_vapid", None)
    yield


# ---------------------------------------------------------------------------
# VAPID key generation/persistence
# ---------------------------------------------------------------------------

def test_get_public_key_generates_and_persists_a_key():
    key1 = push.get_public_key_b64()
    assert isinstance(key1, str)
    assert len(key1) > 40  # real base64url-encoded 65-byte EC point
    assert push.VAPID_KEY_PATH.exists()


def test_get_public_key_is_stable_across_calls():
    key1 = push.get_public_key_b64()
    push._vapid = None  # simulate a fresh process re-reading the persisted key
    key2 = push.get_public_key_b64()
    assert key1 == key2


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------

def test_add_subscription_persists_it():
    push.add_subscription({"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}})
    data = json.loads(push.SUBSCRIPTIONS_PATH.read_text())
    assert len(data) == 1
    assert data[0]["endpoint"] == "https://push.example/abc"


def test_add_subscription_replaces_an_existing_endpoint():
    push.add_subscription({"endpoint": "https://push.example/abc", "keys": {"p256dh": "old", "auth": "y"}})
    push.add_subscription({"endpoint": "https://push.example/abc", "keys": {"p256dh": "new", "auth": "y"}})
    data = json.loads(push.SUBSCRIPTIONS_PATH.read_text())
    assert len(data) == 1
    assert data[0]["keys"]["p256dh"] == "new"


def test_add_subscription_requires_an_endpoint():
    with pytest.raises(ValueError):
        push.add_subscription({"keys": {"p256dh": "x", "auth": "y"}})


def test_remove_subscription_deletes_it():
    push.add_subscription({"endpoint": "https://push.example/abc"})
    push.add_subscription({"endpoint": "https://push.example/def"})
    push.remove_subscription("https://push.example/abc")
    data = json.loads(push.SUBSCRIPTIONS_PATH.read_text())
    assert [s["endpoint"] for s in data] == ["https://push.example/def"]


def test_remove_subscription_is_a_noop_for_an_unknown_endpoint():
    push.add_subscription({"endpoint": "https://push.example/abc"})
    push.remove_subscription("https://push.example/nonexistent")  # must not raise
    data = json.loads(push.SUBSCRIPTIONS_PATH.read_text())
    assert len(data) == 1


# ---------------------------------------------------------------------------
# send_push_notification -- real network call mocked
# ---------------------------------------------------------------------------

def test_send_push_notification_is_a_noop_with_no_subscribers(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "_send_one", lambda *a, **k: calls.append(1))
    asyncio.run(push.send_push_notification("title", "body"))
    assert calls == []


def test_send_push_notification_calls_send_one_per_subscriber(monkeypatch):
    push.add_subscription({"endpoint": "https://push.example/a"})
    push.add_subscription({"endpoint": "https://push.example/b"})
    seen = []

    def fake_send_one(subscription_info, payload, vapid_private_pem):
        seen.append(subscription_info["endpoint"])
        parsed = json.loads(payload)
        assert parsed["title"] == "title"
        assert parsed["body"] == "body"
        return None

    monkeypatch.setattr(push, "_send_one", fake_send_one)
    asyncio.run(push.send_push_notification("title", "body"))
    assert sorted(seen) == ["https://push.example/a", "https://push.example/b"]


def test_send_push_notification_prunes_a_stale_subscription(monkeypatch):
    push.add_subscription({"endpoint": "https://push.example/dead"})
    push.add_subscription({"endpoint": "https://push.example/alive"})

    def fake_send_one(subscription_info, payload, vapid_private_pem):
        return subscription_info["endpoint"] if subscription_info["endpoint"].endswith("dead") else None

    monkeypatch.setattr(push, "_send_one", fake_send_one)
    asyncio.run(push.send_push_notification("title", "body"))
    remaining = json.loads(push.SUBSCRIPTIONS_PATH.read_text())
    assert [s["endpoint"] for s in remaining] == ["https://push.example/alive"]


def test_send_push_notification_never_raises_on_a_send_failure(monkeypatch):
    push.add_subscription({"endpoint": "https://push.example/a"})

    def boom(*args, **kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(push, "_send_one", boom)
    asyncio.run(push.send_push_notification("title", "body"))  # must not raise


def test_send_push_notification_truncates_a_long_body():
    push.add_subscription({"endpoint": "https://push.example/a"})
    seen_payload = {}

    def fake_send_one(subscription_info, payload, vapid_private_pem):
        seen_payload["value"] = json.loads(payload)
        return None

    import unittest.mock

    with unittest.mock.patch.object(push, "_send_one", fake_send_one):
        asyncio.run(push.send_push_notification("title", "x" * 1000))
    assert len(seen_payload["value"]["body"]) == 500
