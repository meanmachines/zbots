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


def test_empty_custom_persona_falls_back_to_default_soul():
    assert persona.with_branding_safety("") == persona.DEFAULT_SOUL
    assert persona.with_branding_safety(None) == persona.DEFAULT_SOUL


def test_custom_persona_gets_the_guardrail_appended():
    result = persona.with_branding_safety("You are a research specialist who cites sources.")
    assert result.startswith("You are a research specialist who cites sources.")
    assert persona.BRANDING_SAFETY in result


def test_guardrail_is_not_duplicated_if_already_present():
    already_safe = f"You are a coding bot.\n\n{persona.BRANDING_SAFETY}"
    result = persona.with_branding_safety(already_safe)
    assert result == already_safe
    assert result.count(persona.BRANDING_SAFETY) == 1
