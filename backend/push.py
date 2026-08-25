"""Real browser push notifications for reminder/routine deliveries --
requested live: "browser push notification... works even if the zBots
tab isn't focused."

Checked hermes-agent's own vendored source first, per instruction, before
building anything new: the desktop app's own notification mechanism
(apps/desktop/src/store/native-notifications.ts) is Electron's native
`Notification` module, driven by IPC from the renderer process -- it has
no web-based equivalent, and there's no VAPID/service-worker/Web-Push
code anywhere in hermes_cli's own Python side either (grepped for
pywebpush/VAPID/push_subscription -- nothing). Nothing to reuse; this is
a genuinely new capability, specific to zBots being a browser-based app
rather than a native desktop shell.

Real Web Push (RFC 8030 push protocol + VAPID, via pywebpush) rather than
a same-tab-only `new Notification(...)` call -- the latter can't fire
once the tab is closed/backgrounded past whatever the OS lets a
background tab do, which defeats "notify me even if I'm not looking."

VAPID key pair is generated once and persisted on the volume via
py_vapid's own Vapid.from_file() (generates + saves if missing, loads
otherwise) -- never regenerated once it exists, since a changed key
silently invalidates every browser's existing subscription (the browser
has no way to know its subscription's key went stale; it just stops
delivering). Subscriptions (one per browser/device that opted in) are
stored in their own small JSON file, same registry convention as
bot_processes.py's own port/worker registry.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

VAPID_KEY_PATH = Path(os.environ.get("PUSH_VAPID_KEY_PATH", "/opt/data/push-vapid-private.pem"))
SUBSCRIPTIONS_PATH = Path(os.environ.get("PUSH_SUBSCRIPTIONS_PATH", "/opt/data/push-subscriptions.json"))
# RFC 8030's own recommendation: a push service may rate-limit or refuse
# a sender with no contact info in its VAPID claims. A real mailto: isn't
# required to be deliverable -- it's just an identifying contact string
# push services see, same convention as a User-Agent header.
VAPID_SUBJECT = os.environ.get("PUSH_VAPID_SUBJECT", "mailto:zbots@example.com")

_vapid = None  # lazily constructed -- see _get_vapid


def _get_vapid():
    global _vapid
    if _vapid is None:
        from py_vapid import Vapid

        VAPID_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _vapid = Vapid.from_file(str(VAPID_KEY_PATH))
    return _vapid


def get_public_key_b64() -> str:
    """The VAPID public key in the raw-uncompressed-point base64url shape
    the browser's `PushManager.subscribe({applicationServerKey: ...})`
    call expects -- NOT the PEM form pywebpush's own send call uses
    server-side; browsers and pywebpush want two different encodings of
    the same key pair, confirmed live against both APIs' own docs.
    """
    import base64
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = _get_vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _read_subscriptions() -> list[dict]:
    if not SUBSCRIPTIONS_PATH.exists():
        return []
    try:
        data = json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_subscriptions(subs: list[dict]) -> None:
    SUBSCRIPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUBSCRIPTIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs, indent=2), encoding="utf-8")
    tmp.replace(SUBSCRIPTIONS_PATH)


def add_subscription(subscription_info: dict) -> None:
    """subscription_info is the browser's own PushSubscription.toJSON()
    shape verbatim ({endpoint, keys: {p256dh, auth}}) -- passed through to
    pywebpush unmodified at send time, so no reshaping here. Re-adding the
    same endpoint (e.g. the browser silently rotated its own keys)
    replaces the old entry rather than duplicating it.
    """
    endpoint = subscription_info.get("endpoint")
    if not endpoint:
        raise ValueError("subscription missing endpoint")
    subs = [s for s in _read_subscriptions() if s.get("endpoint") != endpoint]
    subs.append(subscription_info)
    _write_subscriptions(subs)


def remove_subscription(endpoint: str) -> None:
    subs = _read_subscriptions()
    remaining = [s for s in subs if s.get("endpoint") != endpoint]
    if len(remaining) != len(subs):
        _write_subscriptions(remaining)


def _send_one(subscription_info: dict, payload: str, vapid_private_pem: str) -> Optional[str]:
    """Runs pywebpush's own synchronous (requests-based) send -- call via
    asyncio.to_thread, never directly on the event loop.

    Returns the subscription's endpoint if it should be pruned (the push
    service itself reports the subscription is gone -- 404/410, meaning
    the browser unsubscribed or the device is gone for good), or None if
    the send succeeded or failed for a reason worth just trying again
    next time (network blip, transient 5xx).
    """
    from pywebpush import webpush, WebPushException

    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=vapid_private_pem,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return None
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            return subscription_info.get("endpoint")
        return None


async def send_push_notification(title: str, body: str, url: Optional[str] = None) -> None:
    """Best-effort broadcast to every subscribed browser -- never raises.
    A notification failing to send must never break the chat delivery
    it's attached to (see main.py's own call site: this runs after the
    real message send already succeeded).
    """
    subs = _read_subscriptions()
    if not subs:
        return
    try:
        vapid = _get_vapid()
        private_pem = vapid.private_pem().decode("utf-8")
    except Exception:
        return
    payload = json.dumps({"title": title, "body": body[:500], "url": url or "/bots/"})
    stale: list[str] = []
    for sub in subs:
        try:
            endpoint = await asyncio.to_thread(_send_one, sub, payload, private_pem)
        except Exception:
            endpoint = None
        if endpoint:
            stale.append(endpoint)
    if stale:
        remaining = [s for s in _read_subscriptions() if s.get("endpoint") not in stale]
        _write_subscriptions(remaining)
