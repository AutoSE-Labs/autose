"""DR-010: calibrated context estimates, reserved output, fail-closed overflow."""

from __future__ import annotations

import pytest

from logic.common.context import (
    DEFAULT_MAX_HISTORY_ROUNDS,
    MIN_TOOL_RESULT_CHARS,
    ContextBudget,
    HeuristicTokenEstimator,
)
from logic.common.tool_output import BoundedToolOutput, NullToolOutputStore
from logic.errors import ContextBudgetError


class _FixedEstimator:
    """Charges a flat cost per message, so budgets are exact in tests."""

    def __init__(self, per_message: int = 10) -> None:
        self.per_message = per_message

    def estimate(self, value):
        if isinstance(value, list):
            return max(1, len(value) * self.per_message)
        return 1


def _conversation(rounds: int) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "S"}]
    messages.append({"role": "user", "content": "the request"})
    for index in range(rounds):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "r"})
    return messages


def _keeps_request(messages: list[dict]) -> bool:
    """The request survives, possibly with a compaction note prepended to it."""
    return any(
        message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and message["content"].endswith("the request")
        for message in messages
    )


def test_output_capacity_is_reserved_before_input_budget_is_computed():
    budget = ContextBudget(context_limit=1000, reserved_output_tokens=400)
    prepared = budget.prepare([{"role": "user", "content": "hi"}])
    assert prepared.metrics.input_token_budget == 600
    # Safety factor then shrinks the usable message budget below that.
    assert prepared.metrics.message_budget < 600


def test_safety_factor_shrinks_the_usable_budget():
    strict = ContextBudget(context_limit=1000, reserved_output_tokens=0, safety_factor=2.0)
    lenient = ContextBudget(context_limit=1000, reserved_output_tokens=0, safety_factor=1.0)
    strict_budget = strict.prepare([{"role": "user", "content": "x"}]).metrics.message_budget
    lenient_budget = lenient.prepare([{"role": "user", "content": "x"}]).metrics.message_budget
    assert strict_budget == 500
    assert lenient_budget == 1000


def test_safety_factor_below_one_is_rejected():
    with pytest.raises(ValueError):
        ContextBudget(context_limit=1000, safety_factor=0.9)


def test_reserved_output_consuming_the_whole_window_fails_closed():
    budget = ContextBudget(context_limit=500, reserved_output_tokens=500)
    with pytest.raises(ContextBudgetError, match="Reserved output"):
        budget.prepare([{"role": "user", "content": "x"}])


def test_tool_schemas_consuming_the_whole_window_fail_closed():
    budget = ContextBudget(context_limit=200, reserved_output_tokens=0)
    huge_schema = [{"blob": "x" * 5000}]
    with pytest.raises(ContextBudgetError, match="tool-schema"):
        budget.prepare([{"role": "user", "content": "x"}], huge_schema)


def test_mandatory_content_that_does_not_fit_fails_closed():
    budget = ContextBudget(
        context_limit=15,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )
    with pytest.raises(ContextBudgetError, match="system prompt and current stage input"):
        budget.prepare(_conversation(rounds=4))


def _keeps_request(messages: list[dict]) -> bool:
    """Whether the original request survived, bare or carrying the omission note.

    Since the note moved out of a second system message (Qwen rejects one), it
    is prepended to the request turn itself when rounds are dropped.
    """
    return any(
        (message.get("content") or "") == "the request"
        or (message.get("content") or "").endswith("\n\nthe request")
        for message in messages
    )


def test_a_request_with_no_user_turn_fails_closed():
    budget = ContextBudget(
        context_limit=15,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )
    with pytest.raises(ContextBudgetError, match="no user turn"):
        budget.prepare(
            [{"role": "system", "content": "S"}, {"role": "assistant", "content": "a"}]
        )


def test_fitting_keeps_system_and_latest_user_turn_and_drops_oldest_rounds():
    budget = ContextBudget(
        context_limit=60,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )
    prepared = budget.prepare(_conversation(rounds=5))
    roles = [message["role"] for message in prepared.messages]
    assert roles[0] == "system"
    assert prepared.messages[-1]["role"] == "tool"
    assert _keeps_request(prepared.messages)
    assert len(prepared.messages) < len(_conversation(rounds=5))


def test_dropping_history_keeps_the_same_start_until_it_overflows_again():
    """A window that slides one round per request defeats the prefix cache."""
    budget = ContextBudget(
        context_limit=200,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )
    conversation = _conversation(rounds=12)
    starts = []
    for _ in range(4):
        prepared = budget.prepare(conversation)
        assert budget.estimate(prepared.messages) <= 200
        starts.append(prepared.messages[2]["tool_calls"][0]["id"])
        conversation = conversation + _conversation(rounds=13)[-2:]
        conversation[-2] = dict(conversation[-2], tool_calls=[
            dict(conversation[-2]["tool_calls"][0], id=f"n{len(conversation)}")
        ])
        conversation[-1] = dict(conversation[-1], tool_call_id=f"n{len(conversation)}")

    assert len(set(starts[:2])) == 1


def test_dropping_context_never_orphans_a_tool_result_from_its_call():
    budget = ContextBudget(
        context_limit=200,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )
    prepared = budget.prepare(_conversation(rounds=12))
    pending: set[str] = set()
    for message in prepared.messages:
        if message["role"] == "assistant":
            pending = {call["id"] for call in message.get("tool_calls") or []}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending


def test_a_request_that_already_fits_is_passed_through_unchanged():
    budget = ContextBudget(context_limit=100_000)
    original = _conversation(rounds=2)
    prepared = budget.prepare(original)
    assert prepared.messages == original
    assert prepared.metrics.dropped_message_count == 0


def test_without_a_context_limit_history_is_capped_by_round_count():
    budget = ContextBudget(context_limit=None)
    prepared = budget.prepare(_conversation(rounds=DEFAULT_MAX_HISTORY_ROUNDS + 10))
    assert len(prepared.messages) < len(
        _conversation(rounds=DEFAULT_MAX_HISTORY_ROUNDS + 10)
    )
    assert any(
        "were dropped to stay within" in str(message.get("content"))
        for message in prepared.messages
    )
    assert prepared.metrics.context_limit is None


def test_metrics_report_the_safety_adjusted_estimate():
    budget = ContextBudget(context_limit=10_000, safety_factor=1.5)
    metrics = budget.prepare([{"role": "user", "content": "hello"}]).metrics
    assert metrics.safety_adjusted_input_tokens >= metrics.estimated_input_tokens
    assert metrics.context_safety_factor == 1.5


def test_max_output_tokens_is_only_set_when_a_limit_reserves_capacity():
    assert ContextBudget(context_limit=1000, reserved_output_tokens=256).max_output_tokens == 256
    assert ContextBudget(context_limit=None).max_output_tokens is None
    assert ContextBudget(context_limit=1000, reserved_output_tokens=0).max_output_tokens is None


def test_heuristic_estimator_counts_serialised_structure_not_just_text():
    estimator = HeuristicTokenEstimator()
    bare = estimator.estimate("hello world")
    wrapped = estimator.estimate({"role": "user", "content": "hello world"})
    assert wrapped > bare


# -- a follow-up must not evict what it refers to -------------------------------


def _loop_transcript(rounds: int, *, nudge: str | None = None) -> list[dict]:
    """A request, *rounds* rounds of reading, and optionally a trailing nudge."""
    messages = _conversation(rounds)
    if nudge is not None:
        messages.append({"role": "user", "content": nudge})
    return messages


def test_a_trailing_nudge_does_not_evict_the_request_it_refers_to():
    """The empty-turn nudge (DR-019) arrives *after* the rounds it is about.

    Anchoring the mandatory set on the latest user turn made that nudge the
    whole request: the original ask and every tool result were dropped, so a
    model told "you have already gathered what you need" held nothing and
    started over — the loop that produced this test.
    """
    messages = _loop_transcript(30, nudge="You stopped without producing an answer.")
    budget = ContextBudget(
        context_limit=400,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )

    prepared = budget.prepare(messages)
    contents = [message.get("content") for message in prepared.messages]

    assert _keeps_request(prepared.messages), "the original request was evicted"
    assert contents[-1] == "You stopped without producing an answer."
    # Some evidence survived too, not just the two anchors.
    assert any(message.get("role") == "tool" for message in prepared.messages)


def test_dropping_rounds_around_a_nudge_still_announces_the_omission():
    messages = _loop_transcript(40, nudge="Write your final answer now.")
    budget = ContextBudget(
        context_limit=400,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )

    prepared = budget.prepare(messages)

    assert len(prepared.messages) < len(messages)
    assert any(
        "omitted" in (message.get("content") or "") for message in prepared.messages
    )
    # The note rides on a user turn: Qwen rejects a system message after the first.
    assert [m["role"] for m in prepared.messages].count("system") == 1
    # The anchors survive the omission at both ends.
    assert prepared.messages[-1]["content"] == "Write your final answer now."
    assert _keeps_request(prepared.messages)


def test_an_assistant_turn_keeps_its_tool_replies_when_a_nudge_trails():
    """A dropped round must not leave a tool message without its call."""
    messages = _loop_transcript(20, nudge="Answer now.")
    budget = ContextBudget(
        context_limit=400,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )

    prepared = budget.prepare(messages)

    # Rounds really were dropped, and tool replies really are present — without
    # both, the pairing check below would hold vacuously.
    assert len(prepared.messages) < len(messages)
    tool_messages = [m for m in prepared.messages if m.get("role") == "tool"]
    assert tool_messages

    for index, message in enumerate(prepared.messages):
        if message.get("role") == "tool":
            assert prepared.messages[index - 1]["role"] in ("assistant", "tool")


def test_without_a_nudge_the_request_is_still_the_only_anchor():
    """The ordinary case is unchanged: one user turn, history after it."""
    messages = _loop_transcript(40)
    budget = ContextBudget(
        context_limit=400,
        reserved_output_tokens=0,
        safety_factor=1.0,
        estimator=_FixedEstimator(10),
    )

    prepared = budget.prepare(messages)

    assert prepared.messages[0]["role"] == "system"
    assert _keeps_request(prepared.messages)
    assert prepared.messages[-1]["role"] in ("assistant", "tool")


# -- the tool-output cap has to be read against the window ----------------------
#
# A cap chosen without reference to the window does not bound the context: it
# guarantees the next round evicts what this one gathered. The 4,000-character
# default is ~1,300 estimated tokens, and a served 4k window leaves a message
# budget near 1,000 — so no bounded result could survive a round, and the agent
# re-read what it could no longer see until the no-progress guard stopped it.


def test_a_configured_tool_output_cap_is_narrowed_to_what_the_window_can_carry():
    budget = ContextBudget(context_limit=4096)
    allowance = budget.tool_result_char_allowance(configured=8000)
    assert allowance is not None and allowance < 8000
    # It has to leave room for the system prompt, the request and some history.
    message_budget = budget._message_budget(0)
    assert allowance < message_budget * budget.estimator_chars_per_token


def test_a_cap_that_already_fits_the_window_is_left_alone():
    budget = ContextBudget(context_limit=128_000)
    assert budget.tool_result_char_allowance(configured=8000) == 8000


def test_without_a_declared_window_the_configured_cap_stands():
    """Nothing to measure against means nothing to narrow by."""
    budget = ContextBudget()
    assert budget.tool_result_char_allowance(configured=8000) == 8000


def test_the_allowance_never_falls_below_a_readable_floor():
    """A window this small is a configuration problem, and saying so is
    `MIN_USABLE_CONTEXT_TOKENS`'s job — clipping output to nothing is not."""
    budget = ContextBudget(context_limit=1200)
    assert budget.tool_result_char_allowance(configured=8000) >= MIN_TOOL_RESULT_CHARS


def test_a_clamped_bound_keeps_the_store_and_the_tighter_cap():
    store = NullToolOutputStore()
    bound = BoundedToolOutput(store, max_chars=8000)
    narrowed = bound.clamped(2000)
    assert narrowed is not bound and narrowed.max_chars == 2000
    # Nothing to narrow: the same object, rather than a needless rebuild.
    assert bound.clamped(9000) is bound
    assert bound.clamped(None) is bound
