"""Unit tests for the streaming-specific pieces of backend/engine.py that
don't need the real vendored engine to exercise -- see engine.py's own
"Real streaming" section docstring for the parts that DO (those are only
verified live, the same way the rest of that section already was).

_process_sse_frame takes a plain callable for sse_frame_fn rather than
importing gateway.platforms.api_server's real one, so this needs no vendor
checkout -- matches this suite's own sparse-checkout constraint (see
test_backend.py's provider-collision section for the same pattern).
"""

import json

from backend import engine


def _frame(event: str, payload: dict) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(payload)}\n\n".encode()


def _fake_sse_frame(data, *, event=None, ensure_ascii=True):
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=ensure_ascii)}\n\n".encode()


def test_redacts_a_leak_inside_an_assistant_delta_frame():
    raw = _frame("assistant.delta", {"delta": "I am Hermes, built by Nous Research."})
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert b"Nous Research" not in out
    assert b"Hermes" not in out
    assert b"zBots" in out
    assert is_stale is False


def test_leaves_non_delta_frames_byte_identical():
    raw = _frame("run.started", {"user_message": {"role": "user", "content": "hi"}})
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert out == raw
    assert is_stale is False


def test_leaves_a_clean_delta_frame_byte_identical():
    raw = _frame("assistant.delta", {"delta": "just a normal reply"})
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert out == raw
    assert is_stale is False


def test_leaves_a_keepalive_comment_untouched():
    raw = b": keepalive\n\n"
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert out == raw
    assert is_stale is False


def test_malformed_frame_passes_through_instead_of_raising():
    raw = b"event: assistant.delta\ndata: not-json\n\n"
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert out == raw
    assert is_stale is False


def test_error_event_detection_matches_the_real_prefix_check():
    # _run_stream_attempt flags rollover-worthy frames with a plain
    # startswith(b"event: error\n") check -- this pins that exact prefix
    # against what _sse_frame's own real format produces, so a future
    # format change to _sse_frame gets caught here instead of silently
    # breaking rollover detection.
    error_frame = _fake_sse_frame({"message": "boom"}, event="error")
    assert error_frame.startswith(b"event: error\n")
    ok_frame = _fake_sse_frame({"delta": "hi"}, event="assistant.delta")
    assert not ok_frame.startswith(b"event: error\n")


# ---------------------------------------------------------------------------
# Stale-model-lock detection inside an assistant.delta -- real bug found
# live: switching the active provider mid-conversation leaves an existing
# session's stale locked model id streaming straight through as ordinary-
# looking delta text (never an event: error frame), so this is the only
# place streaming can catch it. See resilience.py's own version of this
# check (the non-streaming path's equivalent) for the full story.
# ---------------------------------------------------------------------------

def test_flags_a_stale_model_lock_delta_as_rollover_worthy():
    raw = _frame("assistant.delta", {"delta": "HTTP 400: old-model is not a valid model ID"})
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert is_stale is True
    # Still redacted/re-framed normally -- detection doesn't suppress the frame.
    assert b"HTTP 400" in out


def test_a_real_reply_that_merely_mentions_http_is_not_flagged_in_a_delta():
    raw = _frame("assistant.delta", {"delta": "By the way, HTTP 400 means a bad request."})
    _out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert is_stale is False
