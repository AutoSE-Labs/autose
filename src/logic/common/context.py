"""Calibrated context budgeting for one inference request (DR-010).

Counting tokens exactly for an arbitrary OpenAI-compatible model is not
possible without that provider's tokenizer, and DR-006 rules out depending on
one. So the estimate here is deliberately a *heuristic*, and DR-010 requires
we treat it as such:

1. Reserve the model's expected output capacity **first**, so a request that
   fits on input alone cannot still fail for having no room to answer in.
2. Divide the remaining budget by a safety factor, buying headroom against the
   local estimate under-counting relative to the real tokenizer.
3. Subtract the tool-schema cost, which is sent on every round.

Whatever survives is the message budget. If the mandatory content — the system
prompt plus the current stage input — does not fit inside it, that is a
:class:`ContextBudgetError`, not something to truncate silently: DR-009 makes a
context-budget failure a typed stage failure.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..errors import ContextBudgetError
from ..limits import count_limit

if TYPE_CHECKING:
    from ..tuning import ContextTuning

__all__ = [
    "TokenEstimator",
    "HeuristicTokenEstimator",
    "ContextMetrics",
    "PreparedRequest",
    "ContextBudget",
    "DEFAULT_SAFETY_FACTOR",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "DEFAULT_MAX_HISTORY_ROUNDS",
    "MIN_RESERVED_OUTPUT_TOKENS",
    "RESERVED_OUTPUT_FRACTION",
    "MIN_USABLE_CONTEXT_TOKENS",
    "TOOL_RESULT_BUDGET_FRACTION",
    "MIN_TOOL_RESULT_CHARS",
    "reserved_output_for",
]

#: Multiplier applied to the local estimate before comparing it against the
#: model's limit. 1.10 buys ~10% headroom for tokenizer drift; raising it
#: trades usable input budget for a smaller chance of overflow.
DEFAULT_SAFETY_FACTOR = 1.10

#: Ceiling on output capacity held back before any input budget is computed
#: (DR-010). Reserving more than this buys nothing on a large window.
DEFAULT_RESERVED_OUTPUT_TOKENS = 8192

#: Floor on reserved output. Below this a model has no room for a useful
#: answer, so a window too small to grant it is a configuration problem.
MIN_RESERVED_OUTPUT_TOKENS = 512

#: Fraction of a known context window reserved for output when the caller does
#: not specify. DR-015: a fixed figure sized for a hosted 128k window swallows
#: a local 4k one whole, making every request fail against exactly the models
#: AutoSE targets.
RESERVED_OUTPUT_FRACTION = 0.25

#: Conversation rounds retained when no numeric context limit is configured.
#:
#: DR-030 leaves this one bounded while removing the caps that stop work: with
#: no declared window there is nothing to measure a request against, and a
#: runtime that silently truncates from the front drops the system prompt
#: first (DR-022). ``-1`` keeps every round, for a repo that knows its window
#: is large but has not declared it.
DEFAULT_MAX_HISTORY_ROUNDS = 20

#: Below this window, AutoSE cannot do useful work and should say so up front
#: (DR-022). The figure is measured, not chosen: a stage sends ~1,300 tokens of
#: tool schema plus a ~460-token system prompt on every round before the user's
#: request or a single file read is counted. At 4,096 that leaves ~1,000 tokens
#: — roughly 3 KB — for the prompt and every file the agent reads, so the first
#: real file read fails the budget. At 8,192 there is about four times that,
#: which is enough to read and change a small file.
MIN_USABLE_CONTEXT_TOKENS = 8192

#: Share of the message budget one tool result may occupy.
#:
#: A cap chosen without reference to the window is the bug this replaces: a
#: fixed 4,000 characters is ~1,300 estimated tokens, which against a served
#: 4k window leaves a message budget of ~1,000 — so *no* bounded result could
#: ever fit, and every round evicted the evidence the round before had
#: gathered. The model then re-read what it had just been given and the
#: no-progress guard stopped it, reporting a stall that was really a budget.
#:
#: A third leaves room for the system prompt, the request, and two or three
#: rounds of gathered evidence alongside the newest result.
TOOL_RESULT_BUDGET_FRACTION = 0.34

#: Floor for that allowance. Below this a result is too clipped to be worth
#: reading at all, and the honest answer is that the window is too small —
#: which `MIN_USABLE_CONTEXT_TOKENS` is what says so.
MIN_TOOL_RESULT_CHARS = 800

def _prepend_notice(message: dict, notice: str) -> dict:
    """Return a copy of a message with a compaction note in its content.

    The note stays out of ``role: system``. A second system message is rejected
    by Qwen with ``System message must be at the beginning``.
    """
    updated = dict(message)
    content = updated.get("content")
    prefix = notice + "\n\n"
    if isinstance(content, str):
        updated["content"] = prefix + content
    elif isinstance(content, list):
        updated["content"] = [{"type": "text", "text": prefix}, *content]
    else:
        updated["content"] = notice
    return updated


_OMISSION_NOTICE = (
    "[Earlier session context and tool exploration were omitted to fit the "
    "configured context window. The canonical stage inputs below remain "
    "authoritative.]"
)


def reserved_output_for(
    context_limit: int | None,
    *,
    fraction: float = RESERVED_OUTPUT_FRACTION,
    minimum: int = MIN_RESERVED_OUTPUT_TOKENS,
    maximum: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
) -> int:
    """Derive how much of *context_limit* to hold back for the answer (DR-015).

    Scales with the window between a floor and a ceiling, so a 4k local model
    keeps a workable share for its output instead of having the whole window
    reserved, while a 256k model does not reserve 64k it will never use.
    """
    if context_limit is None or context_limit <= 0:
        return maximum
    scaled = int(context_limit * fraction)
    return max(minimum, min(scaled, maximum))


@runtime_checkable
class TokenEstimator(Protocol):
    """Strategy seam: how token cost is estimated.

    DR-010 defers exact provider tokenizers rather than rejecting them. When a
    concrete failure justifies the dependency, a tokenizer-backed estimator is
    a new implementation of this Protocol, not a rewrite of the budget logic.
    """

    def estimate(self, value: Any) -> int:
        """Return an approximate token count for *value*."""


class HeuristicTokenEstimator:
    """Character-based estimate over the JSON the request will actually carry.

    Serialising first means structural overhead the model is billed for — role
    keys, tool-call envelopes, escaping — is counted, which a bare
    ``len(text)`` heuristic misses entirely.
    """

    #: Average characters per token. 3 deliberately under-states typical
    #: English (~4) so the raw estimate leans high before the safety factor is
    #: even applied.
    CHARS_PER_TOKEN = 3

    def estimate(self, value: Any) -> int:  # Protocol implementation.
        try:
            serialised = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialised = str(value)
        return max(1, (len(serialised) + 2) // self.CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ContextMetrics:
    """What the budget decided for one request — surfaced for diagnostics."""

    estimated_input_tokens: int
    safety_adjusted_input_tokens: int
    context_safety_factor: float
    context_limit: int | None
    input_token_budget: int | None
    message_budget: int | None
    reserved_output_tokens: int
    estimated_tool_schema_tokens: int
    original_message_count: int
    sent_message_count: int

    @property
    def dropped_message_count(self) -> int:
        return max(0, self.original_message_count - self.sent_message_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_input_tokens": self.estimated_input_tokens,
            "safety_adjusted_input_tokens": self.safety_adjusted_input_tokens,
            "context_safety_factor": self.context_safety_factor,
            "context_limit": self.context_limit,
            "input_token_budget": self.input_token_budget,
            "message_budget": self.message_budget,
            "reserved_output_tokens": self.reserved_output_tokens,
            "estimated_tool_schema_tokens": self.estimated_tool_schema_tokens,
            "original_message_count": self.original_message_count,
            "sent_message_count": self.sent_message_count,
            "dropped_message_count": self.dropped_message_count,
        }


@dataclass(frozen=True)
class PreparedRequest:
    """A message list that fits the budget, plus how that was determined."""

    messages: list[dict] = field(default_factory=list)
    metrics: ContextMetrics | None = None


class ContextBudget:
    """Fits a message list into a model's context window, or refuses to try."""

    def __init__(
        self,
        *,
        context_limit: int | None = None,
        reserved_output_tokens: int | None = None,
        safety_factor: float = DEFAULT_SAFETY_FACTOR,
        estimator: TokenEstimator | None = None,
        max_history_rounds: int | None = DEFAULT_MAX_HISTORY_ROUNDS,
    ) -> None:
        if safety_factor < 1.0:
            raise ValueError("safety_factor must be >= 1.0 to provide headroom")
        self.context_limit = (
            context_limit if context_limit and context_limit > 0 else None
        )
        # None means "scale it to the window" rather than "reserve nothing":
        # 0 stays available as an explicit choice for a caller that wants it.
        self.reserved_output_tokens = (
            reserved_output_for(self.context_limit)
            if reserved_output_tokens is None
            else max(0, reserved_output_tokens)
        )
        self.safety_factor = safety_factor
        self.estimator = estimator or HeuristicTokenEstimator()
        #: ``None`` disables round pruning altogether (DR-030).
        self.max_history_rounds = count_limit(max_history_rounds)

    @classmethod
    def from_tuning(
        cls,
        tuning: ContextTuning,
        *,
        context_limit: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> ContextBudget:
        """Build a budget from a repo's configured tuning (DR-021).

        A default ``ContextTuning()`` reproduces exactly the hardcoded values
        this class used before configuration existed, so an unconfigured repo
        behaves identically.
        """
        return cls(
            context_limit=context_limit,
            reserved_output_tokens=reserved_output_for(
                context_limit,
                fraction=tuning.reserved_output_fraction,
                minimum=tuning.reserved_output_min,
                maximum=tuning.reserved_output_max,
            ),
            safety_factor=tuning.safety_factor,
            estimator=estimator,
            max_history_rounds=tuning.max_history_rounds,
        )

    # -- public API ----------------------------------------------------

    @property
    def max_output_tokens(self) -> int | None:
        """The ``max_tokens`` to send, when a limit reserves output capacity."""
        if self.context_limit is None or self.reserved_output_tokens <= 0:
            return None
        return self.reserved_output_tokens

    def estimate(self, value: Any) -> int:
        return self.estimator.estimate(value)

    def tool_result_char_allowance(
        self, tools: list[dict] | None = None, *, configured: int | None = None
    ) -> int | None:
        """The largest tool result that can survive a round, in characters.

        ``None`` means "do not clamp": either no context limit is declared, or
        *configured* is already inside what the window can carry.

        DR-011 caps tool output so it cannot swamp the window. That cap has to
        be read *against* the window to do its job — a value larger than the
        message budget does not bound the context, it guarantees the budget
        evicts the result on the very next round.
        """
        if self.context_limit is None:
            return configured
        try:
            budget = self._message_budget(self.estimate(tools) if tools else 0)
        except ContextBudgetError:
            # A window too small to hold the tool schemas at all. Nothing this
            # returns can rescue it; the caller's own guard reports it.
            return MIN_TOOL_RESULT_CHARS
        chars = int(
            budget
            * TOOL_RESULT_BUDGET_FRACTION
            * self.estimator_chars_per_token
        )
        allowance = max(MIN_TOOL_RESULT_CHARS, chars)
        if configured is None:
            return allowance
        return min(configured, allowance)

    @property
    def estimator_chars_per_token(self) -> int:
        """Characters per token *as this budget counts them*.

        Deliberately the estimator's own figure rather than a truer one: an
        allowance derived from a different constant than the budget it is
        measured against would not actually fit.
        """
        return int(getattr(self.estimator, "CHARS_PER_TOKEN", 3)) or 3

    def prepare(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> PreparedRequest:
        """Return the largest suffix of *messages* that fits the budget.

        Raises :class:`ContextBudgetError` when no viable request exists —
        never a silently degraded one.
        """
        original_count = len(messages)
        tool_tokens = self.estimate(tools) if tools else 0

        if self.context_limit is None:
            # No declared limit: fall back to a fixed round cap, which bounds
            # growth without pretending to know where the real ceiling is.
            prepared = self._prune_rounds(messages)
            return PreparedRequest(
                messages=prepared,
                metrics=self._metrics(
                    prepared, original_count, tool_tokens, message_budget=None
                ),
            )

        message_budget = self._message_budget(tool_tokens)
        if not messages:
            return PreparedRequest(
                messages=[],
                metrics=self._metrics(
                    [], 0, tool_tokens, message_budget=message_budget
                ),
            )

        if self.estimate(messages) <= message_budget:
            return PreparedRequest(
                messages=list(messages),
                metrics=self._metrics(
                    messages, original_count, tool_tokens, message_budget=message_budget
                ),
            )

        fitted = self._fit(messages, message_budget)
        return PreparedRequest(
            messages=fitted,
            metrics=self._metrics(
                fitted, original_count, tool_tokens, message_budget=message_budget
            ),
        )

    # -- budget arithmetic ---------------------------------------------

    def _message_budget(self, tool_tokens: int) -> int:
        """Reserve output first, then apply the safety margin, then tools."""
        assert self.context_limit is not None
        input_budget = self.context_limit - self.reserved_output_tokens
        if input_budget <= 0:
            raise ContextBudgetError(
                f"Reserved output ({self.reserved_output_tokens} tokens) consumes the "
                f"entire configured context window ({self.context_limit} tokens); "
                "lower reserved_output_tokens or raise context_limit."
            )
        margin_adjusted = math.floor(input_budget / self.safety_factor)
        message_budget = margin_adjusted - tool_tokens
        if message_budget <= 0:
            raise ContextBudgetError(
                "Tool schemas, the safety margin, and reserved output together "
                f"consume the configured context window ({self.context_limit} tokens): "
                f"{tool_tokens} estimated tool-schema tokens leave no room for messages."
            )
        return message_budget

    # -- fitting -------------------------------------------------------

    def _fit(self, messages: list[dict], message_budget: int) -> list[dict]:
        """Drop the oldest optional context until the request fits.

        Three messages are mandatory: the system prompt, the request being
        answered, and the turn the model must respond to now. If those alone
        overflow, the request is impossible and we say so.

        The request and the latest user turn are not always the same message.
        A follow-up appended mid-call — DR-019's empty-turn nudge, most often —
        lands *after* every round it is asking about, so anchoring on the
        latest user turn alone would discard both the original request and the
        evidence that nudge says has already been gathered, leaving the model
        told to answer from nothing. Both ends are pinned instead, and only the
        rounds between them are eligible to be dropped.
        """
        system = (
            messages[:1] if messages and messages[0].get("role") == "system" else []
        )
        user_indices = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if not user_indices:
            raise ContextBudgetError(
                "The request exceeds the configured context window and contains no "
                "user turn that could be preserved."
            )

        request_index, latest_index = user_indices[0], user_indices[-1]
        # Kept in transcript order: the request, then whatever rounds survive,
        # then the turn being responded to. `tail` is empty in the ordinary
        # case where those last two are the same message.
        head = system + [messages[request_index]]
        tail = [messages[latest_index]] if latest_index != request_index else []
        if self.estimate(head + tail) > message_budget:
            raise ContextBudgetError(
                "The system prompt and current stage input alone exceed the "
                f"available message budget ({message_budget} estimated tokens). "
                "Reduce the stage input or configure a larger context_limit."
            )

        end = latest_index if tail else len(messages)
        groups = self._assistant_groups(messages[request_index + 1 : end])
        kept: list[list[dict]] = []
        for group in reversed(groups):
            candidate = (
                head
                + [message for chunk in [group, *kept] for message in chunk]
                + tail
            )
            if self.estimate(candidate) > message_budget:
                break
            kept.insert(0, group)

        recent = [message for group in kept for message in group]
        if len(kept) < len(groups):
            # Qwen rejects any system message that is not the first message.
            noticed = _prepend_notice(messages[request_index], _OMISSION_NOTICE)
            with_notice = system + [noticed] + recent + tail
            if self.estimate(with_notice) <= message_budget:
                return with_notice
        return head + recent + tail

    @staticmethod
    def _assistant_groups(messages: list[dict]) -> list[list[dict]]:
        """Group trailing messages into whole assistant/tool rounds.

        A round is dropped or kept as a unit: an assistant turn carrying
        ``tool_calls`` must never be separated from the ``tool`` messages that
        answer it, or the provider rejects the request outright.

        A ``user`` turn starts a group too, so an intermediate follow-up (a
        first nudge, when a second one has since landed) is dropped or kept
        deliberately rather than falling out of the transcript unnoticed.
        """
        groups: list[list[dict]] = []
        current: list[dict] = []
        for message in messages:
            if message.get("role") in ("assistant", "user"):
                if current:
                    groups.append(current)
                current = [message]
                continue
            if current:
                current.append(message)
        if current:
            groups.append(current)
        return groups

    def _prune_rounds(self, messages: list[dict]) -> list[dict]:
        """Cap history at ``max_history_rounds`` when no token limit is known."""
        if self.max_history_rounds is None or len(messages) <= 1:
            return list(messages)

        head, tail = messages[:1], messages[1:]
        rounds: list[list[dict]] = []
        current: list[dict] = []
        for message in tail:
            if message.get("role") in ("assistant", "user"):
                if current:
                    rounds.append(current)
                current = [message]
            else:
                current.append(message)
        if current:
            rounds.append(current)

        if len(rounds) <= self.max_history_rounds:
            return list(messages)

        dropped = len(rounds) - self.max_history_rounds
        kept = rounds[-self.max_history_rounds :]
        notice = (
            f"[{dropped} earlier conversation round(s) were dropped to stay within "
            "the context window. Rely on information gathered in the remaining "
            "rounds.]"
        )
        kept_messages = [message for group in kept for message in group]
        if kept_messages and kept_messages[0].get("role") == "user":
            kept_messages = [_prepend_notice(kept_messages[0], notice), *kept_messages[1:]]
        else:
            kept_messages = [{"role": "user", "content": notice}, *kept_messages]
        return head + kept_messages

    # -- diagnostics ---------------------------------------------------

    def _metrics(
        self,
        messages: list[dict],
        original_count: int,
        tool_tokens: int,
        *,
        message_budget: int | None,
    ) -> ContextMetrics:
        estimated = self.estimate(messages) + tool_tokens
        return ContextMetrics(
            estimated_input_tokens=estimated,
            safety_adjusted_input_tokens=math.ceil(estimated * self.safety_factor),
            context_safety_factor=self.safety_factor,
            context_limit=self.context_limit,
            input_token_budget=(
                self.context_limit - self.reserved_output_tokens
                if self.context_limit is not None
                else None
            ),
            message_budget=message_budget,
            reserved_output_tokens=self.reserved_output_tokens,
            estimated_tool_schema_tokens=tool_tokens,
            original_message_count=original_count,
            sent_message_count=len(messages),
        )
