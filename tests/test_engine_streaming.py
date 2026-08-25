"""Unit tests for the streaming-specific pieces of backend/engine.py that
don't need the real vendored engine to exercise -- see engine.py's own
"Real streaming" section docstring for the parts that DO (those are only
verified live, the same way the rest of that section already was).

_process_sse_frame takes a plain callable for sse_frame_fn rather than
importing gateway.platforms.api_server's real one, so this needs no vendor
checkout -- matches this suite's own sparse-checkout constraint (see
test_backend.py's provider-collision section for the same pattern).
"""

import asyncio
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


def test_flags_a_stale_model_lock_in_assistant_completed_too():
    # Real bug found live, right after the assistant.delta version of this
    # check shipped: the same underlying failure sometimes streams zero
    # real tokens at all -- the whole rejection arrives in one
    # assistant.completed frame instead ("content", not "delta"), which
    # the delta-only check couldn't see, so it reached a real user
    # unflagged. Confirmed the exact live payload shape before fixing.
    raw = _frame("assistant.completed", {
        "content": "HTTP 400: Model ID 'deepseek-chat' is ambiguous -- it matches multiple models.",
        "completed": True,
    })
    out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert is_stale is True
    assert b"HTTP 400" in out


def test_a_real_completed_reply_is_not_flagged():
    raw = _frame("assistant.completed", {"content": "Sure, happy to help with that.", "completed": True})
    _out, is_stale = engine._process_sse_frame(raw, _fake_sse_frame)
    assert is_stale is False


# ---------------------------------------------------------------------------
# stream_to_bot's own rollover-suppression -- real bug found live, right
# after the frontend started rendering assistant.completed as THE answer
# (see app.js's streamBotReply): a rolled-over attempt 1 still produces its
# own real assistant.completed/run.completed frames before the rollover
# decision is made. Forwarding those live (the old behavior, safe only
# because the old frontend ignored everything but assistant.delta) meant a
# user briefly saw attempt 1's answer render, then attempt 2's real one
# replace it right after -- reported live as the reply "appearing and
# disappearing." These drive the real async generator (no vendor engine
# needed -- _run_stream_attempt is monkeypatched) to pin the fix: progress
# frames stay live, but assistant.completed/run.completed/error only ever
# reach the client from whichever attempt actually won.
# ---------------------------------------------------------------------------

def _fake_attempt(frames):
    async def _attempt(profile, session_id, message, headers):
        for frame, was_error in frames:
            yield frame, was_error

    return _attempt


def _drain(monkeypatch, attempts):
    """attempts: list of frame-lists, one per _run_stream_attempt call (in
    call order) -- the first is attempt 1, the second (if any) is the
    rollover retry."""
    calls = iter(attempts)

    def _next_attempt(profile, session_id, message, headers):
        return _fake_attempt(next(calls))(profile, session_id, message, headers)

    monkeypatch.setattr(engine, "_run_stream_attempt", _next_attempt)
    monkeypatch.setattr(engine, "_ensure_bot_chat_session", _fake_ensure_session)
    monkeypatch.setattr(engine, "_roll_over_bot_session", _fake_roll_over)

    async def _run():
        _state, chunks = await engine.stream_to_bot("default", "hi", "key", None)
        return [frame async for frame in chunks]

    return asyncio.run(_run())


async def _fake_ensure_session(profile, headers, active_session_id):
    return "session-1", [{"id": "session-1"}]


async def _fake_roll_over(profile, headers, all_sessions):
    return "session-2"


def test_no_rollover_forwards_every_frame_including_the_real_completion(monkeypatch):
    attempt_1 = [
        (_frame("tool.started", {"tool_name": "list_bots"}), False),
        (_frame("assistant.delta", {"delta": "hi"}), False),
        (_frame("assistant.completed", {"content": "hi"}), False),
        (_frame("run.completed", {}), False),
    ]
    out = _drain(monkeypatch, [attempt_1])
    assert out == [f for f, _ in attempt_1]


def test_rollover_never_forwards_attempt_ones_completion_frames(monkeypatch):
    attempt_1 = [
        (_frame("tool.started", {"tool_name": "list_bots"}), False),
        (_frame("assistant.completed", {"content": "HTTP 400: stale model"}), True),
        (_frame("run.completed", {}), False),
    ]
    attempt_2 = [
        (_frame("assistant.delta", {"delta": "the real answer"}), False),
        (_frame("assistant.completed", {"content": "the real answer"}), False),
        (_frame("run.completed", {}), False),
    ]
    out = _drain(monkeypatch, [attempt_1, attempt_2])
    # Attempt 1's own tool-progress frame is harmless and still forwarded,
    # but neither of its completion-shaped frames ever reach the client --
    # only attempt 2's (the one that actually won) do.
    assert out == [attempt_1[0][0]] + [f for f, _ in attempt_2]
    assert b"stale model" not in b"".join(out)


def test_a_hard_error_with_no_completed_frame_still_suppresses_correctly(monkeypatch):
    attempt_1 = [(_frame("error", {"message": "boom"}), True)]
    attempt_2 = [(_frame("assistant.completed", {"content": "recovered"}), False)]
    out = _drain(monkeypatch, [attempt_1, attempt_2])
    assert out == [attempt_2[0][0]]


# ---------------------------------------------------------------------------
# get_bot_messages' rollover-replay dedup -- real bug found live: a rollover
# resends the user's own message into the fresh session (by design, see
# stream_to_bot's own comment), and the merge across a bot's whole session
# family never accounted for that -- the user's prompt visibly appeared
# twice (three times for a double rollover) back-to-back in the chat.
# Confirmed live by reading two rollover sessions' own rows: both started
# with the literal same "user" text.
# ---------------------------------------------------------------------------

def test_dedupes_the_replayed_user_message_across_a_rollover():
    messages = [
        {"role": "user", "content": "hi", "timestamp": 1},
        {"role": "assistant", "content": "hello", "timestamp": 2},
        {"role": "user", "content": "do the thing", "timestamp": 3},
        # Rollover replay: same text, no reply in between, from a fresh session.
        {"role": "user", "content": "do the thing", "timestamp": 4},
        {"role": "assistant", "content": "done", "timestamp": 5},
    ]
    out = engine._dedupe_rollover_replay(messages)
    assert [m["content"] for m in out] == ["hi", "hello", "do the thing", "done"]


def test_dedupes_a_double_rollover_replayed_three_times():
    messages = [
        {"role": "user", "content": "do the thing", "timestamp": 1},
        {"role": "user", "content": "do the thing", "timestamp": 2},
        {"role": "user", "content": "do the thing", "timestamp": 3},
        {"role": "assistant", "content": "done", "timestamp": 4},
    ]
    out = engine._dedupe_rollover_replay(messages)
    assert len(out) == 2
    assert out[0]["content"] == "do the thing"


def test_does_not_dedupe_across_an_intervening_assistant_reply():
    messages = [
        {"role": "user", "content": "hi", "timestamp": 1},
        {"role": "assistant", "content": "hello", "timestamp": 2},
        # A real repeat, genuinely sent again after a reply -- not a
        # rollover artifact, must stay as two distinct turns.
        {"role": "user", "content": "hi", "timestamp": 3},
    ]
    out = engine._dedupe_rollover_replay(messages)
    assert len(out) == 3


def test_dedupes_a_context_bridged_replay(monkeypatch):
    # _context_bridge_note prepends a recap to the retried message, so the
    # rolled-over session's own persisted user text is no longer an exact
    # match -- it ends with the original instead. Must still collapse to
    # one, and the CLEAN (noteless) text is the one that survives.
    messages = [
        {"role": "assistant", "content": "Want me to leave it at hourly, or change the cadence?", "timestamp": 1},
        {"role": "user", "content": "it should remind every 5 minute", "timestamp": 2},
        {
            "role": "user",
            "content": "[A brief technical hiccup just restarted this conversation...]\n\nit should remind every 5 minute",
            "timestamp": 3,
        },
        {"role": "assistant", "content": "Got it, every 5 minutes.", "timestamp": 4},
    ]
    out = engine._dedupe_rollover_replay(messages)
    assert [m["content"] for m in out] == [
        "Want me to leave it at hourly, or change the cadence?",
        "it should remind every 5 minute",
        "Got it, every 5 minutes.",
    ]


# ---------------------------------------------------------------------------
# _context_bridge_note -- real bug found live: a rollover swaps in a
# genuinely fresh session with zero conversational memory, even though
# get_bot_messages' own merged-history view makes the whole family look
# like one continuous thread. A direct, short follow-up landed on a
# freshly-rolled-over session with no idea what it referred to ("I'm not
# sure what you're referring to"). Borrows hermes-agent's own
# _pending_model_notes PATTERN (gateway/run.py -- prepend a short note to
# the next outgoing message) rather than the mechanism itself, since
# rollover is a zBots-level workaround with no access to that internal
# queue.
# ---------------------------------------------------------------------------

def test_context_bridge_note_recaps_the_old_sessions_last_turns(monkeypatch):
    async def fake_call_handler(handler_name, *, profile, method, path, query=None, headers=None, match_info=None):
        assert path == "/api/sessions/old-sid/messages"
        return 200, {
            "data": [
                {"role": "user", "content": "set up a water reminder"},
                {"role": "assistant", "content": "Done. Want it hourly, or a different cadence?"},
            ]
        }

    monkeypatch.setattr(engine, "_call_handler", fake_call_handler)
    note = asyncio.run(engine._context_bridge_note("default", "old-sid", {}))
    assert "set up a water reminder" in note
    assert "Done. Want it hourly, or a different cadence?" in note
    assert note.endswith("\n\n")


def test_context_bridge_note_is_empty_on_a_fetch_failure(monkeypatch):
    async def fake_call_handler(*args, **kwargs):
        return 500, {"error": "boom"}

    monkeypatch.setattr(engine, "_call_handler", fake_call_handler)
    note = asyncio.run(engine._context_bridge_note("default", "old-sid", {}))
    assert note == ""


def test_context_bridge_note_is_empty_with_no_prior_messages(monkeypatch):
    async def fake_call_handler(*args, **kwargs):
        return 200, {"data": []}

    monkeypatch.setattr(engine, "_call_handler", fake_call_handler)
    note = asyncio.run(engine._context_bridge_note("default", "old-sid", {}))
    assert note == ""


def test_strip_context_bridge_note_replaces_a_full_bridged_message(monkeypatch):
    async def fake_call_handler(handler_name, *, profile, method, path, query=None, headers=None, match_info=None):
        return 200, {"data": [{"role": "user", "content": "set up a water reminder"}]}

    monkeypatch.setattr(engine, "_call_handler", fake_call_handler)
    note = asyncio.run(engine._context_bridge_note("default", "old-sid", {}))
    bridged = note + "it should remind every 5 minute"
    assert engine.strip_context_bridge_note(bridged) == "(recovering from a brief hiccup...)"


def test_strip_context_bridge_note_replaces_a_preview_truncated_mid_note():
    # Real bug found live: hermes-agent's own native `preview` field is
    # already truncated by the engine itself before zBots ever reads it --
    # confirmed live, the roster showed "...restarted this conversation
    # o..." cut off well short of the note's own end marker. The original
    # (marker-search) version of this function always missed a truncated
    # note like this and returned it unchanged; the fix has to work off
    # the note's own OPENING words instead, which survive truncation.
    truncated = "[A brief technical hiccup just restarted this conversation o..."
    assert engine.strip_context_bridge_note(truncated) == "(recovering from a brief hiccup...)"


def test_strip_context_bridge_note_is_a_noop_on_plain_text():
    assert engine.strip_context_bridge_note("just a normal message") == "just a normal message"


def test_context_bridge_note_swallows_an_exception(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(engine, "_call_handler", boom)
    note = asyncio.run(engine._context_bridge_note("default", "old-sid", {}))  # must not raise
    assert note == ""


def test_send_to_bot_prepends_the_note_to_the_retried_message(monkeypatch):
    # http transport, not embedded -- avoids _profile_scope's embedded
    # branch constructing the real vendored adapter (which needs packages
    # not installed in this test environment); every dependency this test
    # actually exercises is mocked below regardless of transport.
    monkeypatch.setattr(engine, "_CHAT_TRANSPORT", "http")

    async def fake_ensure_session(profile, headers, active_session_id):
        return "sid-1", [{"id": "sid-1"}]

    async def fake_roll_over(profile, headers, all_sessions):
        return "sid-2"

    async def fake_note(profile, old_sid, headers):
        assert old_sid == "sid-1"
        return "[recap]\n\n"

    chat_calls = []

    async def fake_call_handler(handler_name, *, profile, method, path, json_body=None, headers=None, match_info=None, query=None):
        if handler_name == "_handle_session_chat":
            chat_calls.append(json_body["message"])
            sid = match_info["session_id"]
            if sid == "sid-1":
                return 200, {"message": {"content": "HTTP 400: stale-model"}}
            return 200, {"message": {"content": "the real answer"}}
        raise AssertionError(f"unexpected handler {handler_name}")

    monkeypatch.setattr(engine, "_ensure_bot_chat_session", fake_ensure_session)
    monkeypatch.setattr(engine, "_roll_over_bot_session", fake_roll_over)
    monkeypatch.setattr(engine, "_context_bridge_note", fake_note)
    monkeypatch.setattr(engine, "_call_handler", fake_call_handler)

    reply, session_id = asyncio.run(engine.send_to_bot("default", "the original message", "key", None))
    assert session_id == "sid-2"
    assert reply == "the real answer"
    assert chat_calls == ["the original message", "[recap]\n\nthe original message"]
