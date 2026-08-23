"""Unit tests for the pluggable resilience checks (backend/resilience.py).

These are pure functions with no engine/session dependency, so unlike the
retry/rollover behavior in engine.py's send_to_bot (exercised live against
a real deployment -- see its own docstring), the failure-detection logic
itself can be tested directly, in isolation, with no mocking at all.
"""

from backend import resilience as r


def test_server_error_asks_for_rollover():
    decision = r.server_error_rolls_over(status=500, body={"error": "boom"}, reply="")
    assert decision.mode is r.RetryMode.ROLLOVER


def test_client_error_does_not_roll_over():
    decision = r.server_error_rolls_over(status=404, body=None, reply="")
    assert decision.mode is r.RetryMode.NONE


def test_success_status_does_not_roll_over():
    decision = r.server_error_rolls_over(status=200, body={}, reply="fine")
    assert decision.mode is r.RetryMode.NONE


def test_corrupted_reply_asks_for_same_session_retry():
    corrupted = "<unused12> <unused12> <unused12> some text"
    decision = r.corrupted_reply_retries_same_session(status=200, body={}, reply=corrupted)
    assert decision.mode is r.RetryMode.SAME_SESSION


def test_clean_reply_does_not_retry():
    decision = r.corrupted_reply_retries_same_session(status=200, body={}, reply="a normal reply")
    assert decision.mode is r.RetryMode.NONE


def test_short_unused_token_run_does_not_count_as_corrupted():
    # The pattern requires 3+ repeats -- two is (per the upstream bug this
    # guards against) not the corruption signature, just an unlucky token.
    decision = r.corrupted_reply_retries_same_session(
        status=200, body={}, reply="<unused1> <unused1> normal text after"
    )
    assert decision.mode is r.RetryMode.NONE


def test_corruption_check_is_skipped_on_error_status():
    # A >=400 status means there's no real reply to judge for corruption --
    # server_error_rolls_over is the check that owns that case instead.
    corrupted_looking = "<unused1> <unused1> <unused1>"
    decision = r.corrupted_reply_retries_same_session(status=500, body=None, reply=corrupted_looking)
    assert decision.mode is r.RetryMode.NONE


def test_evaluate_runs_checks_in_order_and_returns_first_match():
    decision = r.evaluate(status=500, body={"error": "x"}, reply="")
    assert decision.mode is r.RetryMode.ROLLOVER
    assert decision.reason == "server error"


def test_evaluate_falls_through_to_corruption_check():
    decision = r.evaluate(status=200, body={}, reply="<unused9> <unused9> <unused9>")
    assert decision.mode is r.RetryMode.SAME_SESSION
    assert decision.reason == "corrupted reply"


def test_evaluate_returns_none_when_nothing_matches():
    decision = r.evaluate(status=200, body={}, reply="a perfectly normal reply")
    assert decision.mode is r.RetryMode.NONE


def test_registry_is_exactly_the_two_documented_checks():
    # Guards against a check being added/removed silently -- this list is
    # the actual plugin registry, so its contents are part of the contract.
    names = [check.name for check in r.RESILIENCE_CHECKS]
    assert names == ["server_error_rollover", "corrupted_reply_retry"]
