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


def test_registry_is_exactly_the_three_documented_checks():
    # Guards against a check being added/removed silently -- this list is
    # the actual plugin registry, so its contents are part of the contract.
    names = [check.name for check in r.RESILIENCE_CHECKS]
    assert names == ["server_error_rollover", "stale_model_lock_rollover", "corrupted_reply_retry"]


# ---------------------------------------------------------------------------
# stale_model_lock_rolls_over -- real bug found live: switching the global
# active provider (Models page) doesn't clear an existing session's own
# locked model id, so its next message sends the OLD provider's stale model
# string to the NEW provider. A reachable-but-confused provider answers 200
# with its own rejection delivered AS the reply text instead of a real HTTP
# failure, so this has to key off reply shape, not status.
# ---------------------------------------------------------------------------

def test_stale_model_lock_reply_asks_for_rollover():
    reply = "HTTP 400: nvidia/Qwen3.6-35B-A3B-NVFP4 is not a valid model ID"
    decision = r.stale_model_lock_rolls_over(status=200, body={}, reply=reply)
    assert decision.mode is r.RetryMode.ROLLOVER


def test_stale_model_lock_catches_a_differently_worded_provider_error_too():
    # Real bug found live: the first version of this check only matched the
    # "not a valid model" wording and missed this second, differently
    # worded rejection from a different provider on the same class of
    # failure -- the fix is matching on shape (starts with "HTTP <code>:"),
    # not on any one provider's specific error text.
    reply = "HTTP 400: Model ID 'deepseek-chat' is ambiguous -- it matches multiple models: deepseek/deepseek-chat, deepseek/deepseek-chat-v2.5."
    decision = r.stale_model_lock_rolls_over(status=200, body={}, reply=reply)
    assert decision.mode is r.RetryMode.ROLLOVER


def test_a_real_reply_that_merely_mentions_http_is_not_flagged():
    # The anchor is deliberately strict (reply must START with "HTTP <code>:
    # ... not a valid model") -- a real answer that happens to discuss HTTP
    # status codes mid-sentence must not be mistaken for this failure.
    reply = "By the way, HTTP 400 means the request was malformed."
    decision = r.stale_model_lock_rolls_over(status=200, body={}, reply=reply)
    assert decision.mode is r.RetryMode.NONE


def test_stale_model_lock_check_is_skipped_on_error_status():
    # Mirrors corrupted_reply's own guard -- a >=400 status means
    # server_error_rolls_over already owns that case.
    reply = "HTTP 400: some-model is not a valid model ID"
    decision = r.stale_model_lock_rolls_over(status=500, body=None, reply=reply)
    assert decision.mode is r.RetryMode.NONE


def test_evaluate_catches_stale_model_lock_before_corruption_check():
    reply = "HTTP 400: old-provider/some-model is not a valid model ID"
    decision = r.evaluate(status=200, body={}, reply=reply)
    assert decision.mode is r.RetryMode.ROLLOVER
    assert decision.reason == "stale model lock"
