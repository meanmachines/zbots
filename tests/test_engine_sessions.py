"""Unit tests for backend/engine.py's _ensure_bot_chat_session -- in
particular its `force_new` parameter, added for a real bug found live: a
`task`-category bot (main.py's own concept) is supposed to always start a
brand-new session, and originally achieved that by passing
active_session_id=None. That alone doesn't work once ANY session already
exists for the profile -- the pre-existing fallback below reuses
`all_sessions[-1]` (a title-family search, meant for "state was lost,
recover the real session") regardless of what active_session_id was.
Confirmed live: a second message to a real task bot landed back on the
first message's own session, and a secret told to it in message 1 came
right back in message 2's reply. force_new=True is the real fix -- see
_ensure_bot_chat_session's own docstring in engine.py.
"""

import asyncio

from backend import engine


def _session(sid: str, title: str) -> dict:
    return {"id": sid, "title": title}


def test_no_active_id_reuses_the_latest_session_when_one_exists(monkeypatch):
    # This is the pre-existing, still-correct behavior for a NON-task bot
    # (or a task bot's very first call before force_new existed) --
    # active_session_id=None recovers state by reusing the latest real
    # session, it does not start fresh.
    async def fake_list(profile, headers):
        return [_session("sid-1", "[Bots UI] alpha"), _session("sid-2", "[Bots UI] alpha #2")]

    monkeypatch.setattr(engine, "_list_bot_sessions", fake_list)

    session_id, all_sessions = asyncio.run(engine._ensure_bot_chat_session("alpha", {}, None))
    assert session_id == "sid-2"
    assert len(all_sessions) == 2


def test_no_active_id_creates_a_session_when_none_exist(monkeypatch):
    async def fake_list(profile, headers):
        return []

    created = []

    async def fake_create(profile, title, headers):
        created.append(title)
        return "sid-new"

    monkeypatch.setattr(engine, "_list_bot_sessions", fake_list)
    monkeypatch.setattr(engine, "_create_bot_session", fake_create)

    session_id, all_sessions = asyncio.run(engine._ensure_bot_chat_session("alpha", {}, None))
    assert session_id == "sid-new"
    assert created == ["[Bots UI] alpha"]


def test_force_new_ignores_an_existing_session_and_starts_fresh(monkeypatch):
    # The real fix: force_new=True must NOT return all_sessions[-1] even
    # though a session already exists for this profile.
    async def fake_list(profile, headers):
        return [_session("sid-1", "[Bots UI] quick-answers")]

    roll_over_calls = []

    async def fake_roll_over(profile, headers, all_sessions):
        roll_over_calls.append(all_sessions)
        return "sid-2"

    monkeypatch.setattr(engine, "_list_bot_sessions", fake_list)
    monkeypatch.setattr(engine, "_roll_over_bot_session", fake_roll_over)

    session_id, all_sessions = asyncio.run(
        engine._ensure_bot_chat_session("quick-answers", {}, None, force_new=True)
    )
    assert session_id == "sid-2"
    assert session_id != "sid-1"
    assert len(roll_over_calls) == 1


def test_force_new_ignores_a_supplied_active_session_id_too(monkeypatch):
    # Even a real, currently-valid active_session_id must not be reused --
    # force_new is an unconditional "start fresh" signal.
    async def fake_list(profile, headers):
        return [_session("sid-1", "[Bots UI] quick-answers")]

    async def fake_roll_over(profile, headers, all_sessions):
        return "sid-2"

    monkeypatch.setattr(engine, "_list_bot_sessions", fake_list)
    monkeypatch.setattr(engine, "_roll_over_bot_session", fake_roll_over)

    session_id, _ = asyncio.run(
        engine._ensure_bot_chat_session("quick-answers", {}, "sid-1", force_new=True)
    )
    assert session_id == "sid-2"


def test_force_new_still_creates_fresh_when_no_session_exists_yet(monkeypatch):
    async def fake_list(profile, headers):
        return []

    async def fake_create(profile, title, headers):
        return "sid-first"

    monkeypatch.setattr(engine, "_list_bot_sessions", fake_list)
    monkeypatch.setattr(engine, "_create_bot_session", fake_create)

    session_id, all_sessions = asyncio.run(
        engine._ensure_bot_chat_session("quick-answers", {}, None, force_new=True)
    )
    assert session_id == "sid-first"
    assert all_sessions == [{"id": "sid-first"}]


def test_force_new_session_result_stays_within_the_same_title_family(monkeypatch):
    # A forced-fresh session for a task bot must still merge into the
    # bot's one continuous visible thread (get_bot_messages merges every
    # rollover-numbered session back together) -- only the MODEL's own
    # context should be isolated, not the user-visible history.
    async def fake_list(profile, headers):
        return [_session("sid-1", "[Bots UI] quick-answers")]

    async def fake_roll_over(profile, headers, all_sessions):
        return "sid-2"

    monkeypatch.setattr(engine, "_list_bot_sessions", fake_list)
    monkeypatch.setattr(engine, "_roll_over_bot_session", fake_roll_over)

    _, all_sessions = asyncio.run(
        engine._ensure_bot_chat_session("quick-answers", {}, None, force_new=True)
    )
    new_entry = next(s for s in all_sessions if s["id"] == "sid-2")
    assert engine._bot_session_rollover_n(new_entry["title"], "quick-answers") == 2
