"""Unit tests for the shared branding-safe persona (backend/persona.py).

See its own module docstring for the incident that made this necessary:
the stock default soul literally reads "You are Hermes Agent... created
by Nous Research," and a bot with no create_bot tool improvised a broken
bot via raw filesystem/CLI access while narrating the underlying engine's
internals to the user.
"""

from backend import persona


def test_default_soul_does_not_self_identify_as_hermes():
    # BRANDING_SAFETY itself has to name "Hermes"/"Nous Research" to warn
    # against using them, so this can't just assert the words are absent
    # (that would fail on the guardrail text itself, which is correct and
    # intended). What actually matters -- and what the real incident was
    # -- is that the persona never claims to BE Hermes/made by Nous.
    lowered = persona.DEFAULT_SOUL.lower()
    assert "you are hermes" not in lowered
    assert "created by nous research" not in lowered
    assert "zbots" in lowered


def test_default_soul_contains_the_branding_safety_guardrail():
    assert persona.BRANDING_SAFETY in persona.DEFAULT_SOUL


def test_default_soul_contains_the_response_style_guardrail():
    assert persona.RESPONSE_STYLE in persona.DEFAULT_SOUL


def test_empty_custom_persona_falls_back_to_default_soul():
    assert persona.with_branding_safety("") == persona.DEFAULT_SOUL
    assert persona.with_branding_safety(None) == persona.DEFAULT_SOUL


def test_custom_persona_gets_both_guardrails_appended():
    result = persona.with_branding_safety("You are a research specialist who cites sources.")
    assert result.startswith("You are a research specialist who cites sources.")
    assert persona.BRANDING_SAFETY in result
    assert persona.RESPONSE_STYLE in result


def test_guardrails_are_not_duplicated_if_already_present():
    already_safe = f"You are a coding bot.\n\n{persona.BRANDING_SAFETY}\n\n{persona.RESPONSE_STYLE}"
    result = persona.with_branding_safety(already_safe)
    assert result == already_safe
    assert result.count(persona.BRANDING_SAFETY) == 1
    assert result.count(persona.RESPONSE_STYLE) == 1


def test_branding_safety_present_but_response_style_missing_only_adds_the_missing_one():
    partial = f"You are a coding bot.\n\n{persona.BRANDING_SAFETY}"
    result = persona.with_branding_safety(partial)
    assert result.count(persona.BRANDING_SAFETY) == 1
    assert result.count(persona.RESPONSE_STYLE) == 1


# ---------------------------------------------------------------------------
# redact_branding_leaks -- the deterministic backstop. Cases below are the
# actual replies observed live: the persona instruction alone leaked
# "Hermes"/"Nous Research" in 2 of 3 identical requests despite the correct
# soul being in effect (verified separately) -- a probabilistic model
# choosing to disclose something it was told not to isn't fixable by
# rewording the instruction, hence this scrub.
# ---------------------------------------------------------------------------

def test_redacts_hermes_agent_created_by_nous_research():
    reply = "I am a zBots bot created by MeanMachines Technologies, running on Hermes Agent."
    result = persona.redact_branding_leaks(reply)
    assert "Hermes" not in result
    assert "zBots" in result


def test_redacts_hermes_agent_by_nous_research_together():
    reply = "I'm a bot on zBots, and I run on Hermes Agent by Nous Research."
    result = persona.redact_branding_leaks(reply)
    assert "Hermes" not in result
    assert "Nous" not in result
    assert "MeanMachines Technologies" in result


def test_clean_reply_passes_through_unchanged():
    reply = "I'm a zBots bot created by MeanMachines Technologies."
    assert persona.redact_branding_leaks(reply) == reply


def test_empty_reply_passes_through():
    assert persona.redact_branding_leaks("") == ""
    assert persona.redact_branding_leaks(None) is None


def test_redacts_case_insensitively():
    assert "hermes" not in persona.redact_branding_leaks("i run on HERMES").lower()


def test_redacts_home_directory_path():
    result = persona.redact_branding_leaks("config lives at ~/.hermes/config.yaml")
    assert "~/.hermes" not in result


# ---------------------------------------------------------------------------
# scrub_branding_deep -- the same backstop applied to hermes-agent's own
# native dashboard-API responses proxied straight through by main.py's
# /connectors, /skills, /mcp/catalog, /plugins (a real audit found 27 of 33
# connector platforms, plus skills/catalog/plugin entries, carrying
# hermes-agent's own description/prompt/help text verbatim -- see this
# function's own comment in persona.py for the incident).
# ---------------------------------------------------------------------------

def test_scrub_branding_deep_redacts_a_string_value_in_a_dict():
    data = {"name": "Discord", "description": "Connect Hermes to Discord DMs."}
    result = persona.scrub_branding_deep(data)
    assert "Hermes" not in result["description"]
    assert result["name"] == "Discord"


def test_scrub_branding_deep_recurses_into_nested_lists_and_dicts():
    data = {"platforms": [{"description": "Use Hermes via Matrix."}, {"description": "clean"}]}
    result = persona.scrub_branding_deep(data)
    assert "Hermes" not in result["platforms"][0]["description"]
    assert result["platforms"][1]["description"] == "clean"


def test_scrub_branding_deep_leaves_docs_url_untouched():
    # Real reason this exists: scrubbing "hermes-agent" out of
    # "hermes-agent.nousresearch.com" would silently turn a working setup
    # doc link into a broken domain -- worse than leaving the real one.
    data = {"docs_url": "https://hermes-agent.nousresearch.com/docs/x", "description": "Use Hermes."}
    result = persona.scrub_branding_deep(data)
    assert result["docs_url"] == "https://hermes-agent.nousresearch.com/docs/x"
    assert "Hermes" not in result["description"]


def test_scrub_branding_deep_passes_through_non_string_scalars():
    data = {"enabled": True, "count": 3, "note": None}
    assert persona.scrub_branding_deep(data) == data


def test_scrub_branding_deep_handles_a_bare_list():
    result = persona.scrub_branding_deep(["Use Hermes here.", "clean"])
    assert "Hermes" not in result[0]
    assert result[1] == "clean"
