"""Pluggable resilience checks for the chat call path (see engine.py's
send_to_bot). Each check inspects one completed chat attempt and decides
whether the underlying engine's known failure modes are present -- the
retry/rollover sequencing itself stays in engine.py (it depends on
session bookkeeping this module has no business touching), but *what
counts as a failure worth retrying* lives here, as small, independently
testable, swappable units.

This is the "session" category of zBots' plugin-registry design: a
Python-native pattern inspired by -- not dependent on -- DeepSeek
Harness's Cordis plugin kernel. A new provider- or deployment-specific
failure signature plugs in as one more function added to
RESILIENCE_CHECKS; nothing else in the chat path needs to change to pick
it up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol


class RetryMode(Enum):
    NONE = "none"           # accept this attempt's result as final
    SAME_SESSION = "same"   # retry the same session (a single bad turn)
    ROLLOVER = "rollover"   # start a fresh session and retry there


@dataclass(frozen=True)
class RetryDecision:
    mode: RetryMode
    reason: str = ""


class ResilienceCheck(Protocol):
    """One named check against a completed chat attempt."""

    name: str

    def evaluate(self, *, status: int, body: Optional[dict], reply: str) -> RetryDecision: ...


@dataclass(frozen=True)
class FunctionCheck:
    """Adapts a plain function into a ResilienceCheck. Covers every check
    below -- a class implementing the protocol directly is only needed for
    a check that has to carry its own state between calls, which none of
    these do."""

    name: str
    fn: Callable[..., RetryDecision]

    def evaluate(self, *, status: int, body: Optional[dict], reply: str) -> RetryDecision:
        return self.fn(status=status, body=body, reply=reply)


def server_error_rolls_over(*, status: int, body: Optional[dict], reply: str) -> RetryDecision:
    """Real, recurring bug (not a one-off, lives inside the engine's own
    agent_init.py/api_server.py): a session that answers correctly on its
    first turn can start failing on every turn after -- once a session's
    model field gets persisted as a real provider model string (which
    happens automatically after its first turn), the next turn's resolver
    re-reads that stored string and tries to resolve it as a route alias
    instead of a raw model id, fails, and falls through to an
    unconfigured placeholder. A fresh session (kept, not deleted --
    get_bot_messages merges every rollover back into one continuous
    thread) sidesteps it entirely.
    """
    if status >= 500:
        return RetryDecision(RetryMode.ROLLOVER, "server error")
    return RetryDecision(RetryMode.NONE)


_STALE_MODEL_LOCK_RE = re.compile(r"^HTTP \d{3}:")


def stale_model_lock_rolls_over(*, status: int, body: Optional[dict], reply: str) -> RetryDecision:
    """A variant of server_error_rolls_over's own bug, not a new one: a
    session created under one provider persists its model id, and after
    the *global* active provider is switched (Models page, /api/model/set)
    that stale id gets sent to the NEW provider on this session's next
    turn. When the new provider is unreachable/misconfigured that surfaces
    as a >=500 (server_error_rolls_over already catches it); when the
    provider is reachable but simply doesn't recognize the stale model id,
    it instead answers 200 with its own rejection delivered AS the reply
    text -- confirmed live twice, with two DIFFERENT wordings from two
    different providers ("HTTP 400: <old-model> is not a valid model ID"
    switching to OpenRouter's Nemotron; "HTTP 400: Model ID 'deepseek-chat'
    is ambiguous -- it matches multiple models" on a session that predated
    an OpenRouter model catalog change). The first version of this check
    matched only the first exact wording and missed the second live in
    production -- status alone can't distinguish either from a real reply,
    and neither can a specific error phrase, since the provider decides the
    wording, not this code. The only thing every real assistant reply
    reliably never does is open with the literal string "HTTP <code>:", so
    that's the actual check, deliberately broader than any one message.
    """
    if status < 400 and _STALE_MODEL_LOCK_RE.match((reply or "").strip()):
        return RetryDecision(RetryMode.ROLLOVER, "stale model lock")
    return RetryDecision(RetryMode.NONE)


_STREAM_CORRUPTION_RE = re.compile(r"(<unused\d+>\s*){3,}")


def corrupted_reply_retries_same_session(
    *, status: int, body: Optional[dict], reply: str
) -> RetryDecision:
    """Detects the garbage-token artifact from a known upstream bug where
    the engine cancels its internal LLM stream after ~1.5s of silence
    during prefill -- see vendor/VENDORED_COMMIT.md and the upstream
    issue tracked there. The corrupted reply is otherwise indistinguishable
    from a real one, so this is the only place it can be caught. Not a
    wedged session, just a bad single turn, so this asks for a same-session
    retry, not a rollover.
    """
    if status < 400 and _STREAM_CORRUPTION_RE.search(reply or ""):
        return RetryDecision(RetryMode.SAME_SESSION, "corrupted reply")
    return RetryDecision(RetryMode.NONE)


RESILIENCE_CHECKS: list[ResilienceCheck] = [
    FunctionCheck("server_error_rollover", server_error_rolls_over),
    FunctionCheck("stale_model_lock_rollover", stale_model_lock_rolls_over),
    FunctionCheck("corrupted_reply_retry", corrupted_reply_retries_same_session),
]


def evaluate(*, status: int, body: Optional[dict], reply: str) -> RetryDecision:
    """Run every registered check in order; the first non-NONE decision
    wins. Checks are independent by design -- order only matters when more
    than one could plausibly fire on the same attempt, which doesn't
    happen with the two registered here (a >=500 response never carries a
    parseable reply for the corruption check to look at)."""
    for check in RESILIENCE_CHECKS:
        decision = check.evaluate(status=status, body=body, reply=reply)
        if decision.mode is not RetryMode.NONE:
            return decision
    return RetryDecision(RetryMode.NONE)
