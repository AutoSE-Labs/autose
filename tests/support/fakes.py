"""Test doubles for the Strategy seams in ``src/logic``.

Every seam DR-002 requires exists precisely so a second implementation can be
dropped in without editing its callers. These fakes are that second
implementation for tests: a scripted ``ChatModel``, a recording approval gate,
and helpers for building tool calls.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from logic.common.approval import ApprovalDecision, ApprovalRequest
from logic.inference.types import CompletionResult, StreamChunk, ToolCall, Usage


def tool_call(name: str, call_id: str = "call_0", **arguments) -> ToolCall:
    """Build a normalised tool call the way a provider response would."""
    import json

    return ToolCall(
        id=call_id,
        name=name,
        arguments=dict(arguments),
        raw_arguments=json.dumps(arguments),
    )


def answer(content: str, *, prompt_tokens: int = 0, completion_tokens: int = 0):
    return CompletionResult(
        content=content,
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        finish_reason="stop",
    )


def calls(*tool_calls: ToolCall, content: str = "") -> CompletionResult:
    return CompletionResult(
        content=content, tool_calls=tuple(tool_calls), finish_reason="tool_calls"
    )


class ScriptedChatModel:
    """A ``ChatModel`` that replays a fixed list of completions.

    Records every request it was given, so a test can assert on what the agent
    actually sent — which messages survived budgeting, which tool schemas were
    offered.
    """

    def __init__(
        self, script: Sequence[CompletionResult], *, model: str = "test-model"
    ):
        self._script = list(script)
        self._model = model
        self.requests: list[dict] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def _next(
        self, messages, tools, max_output_tokens, reasoning_effort=None
    ) -> CompletionResult:
        self.requests.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
                "max_output_tokens": max_output_tokens,
                "reasoning_effort": reasoning_effort,
            }
        )
        if not self._script:
            raise AssertionError("ScriptedChatModel ran out of scripted completions")
        item = self._script.pop(0)
        # A scripted exception is raised in place of a completion, which is how
        # a test stands in for a runtime that fails one particular request.
        if isinstance(item, BaseException):
            raise item
        return item

    def complete(
        self, messages, *, tools=None, max_output_tokens=None, reasoning_effort=None
    ):
        return self._next(messages, tools, max_output_tokens, reasoning_effort)

    def stream(
        self, messages, *, tools=None, max_output_tokens=None, reasoning_effort=None
    ) -> Iterator[StreamChunk]:
        result = self._next(messages, tools, max_output_tokens, reasoning_effort)
        if result.content:
            for piece in _split(result.content):
                yield StreamChunk(content=piece)
        yield StreamChunk(result=result)


def _split(text: str, size: int = 8) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


class RecordingApprovalGate:
    """Approves or denies on a fixed schedule, recording every request."""

    def __init__(self, *decisions: bool, default: bool = False) -> None:
        self._decisions = list(decisions)
        self._default = default
        self.requests: list[ApprovalRequest] = []

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        allowed = self._decisions.pop(0) if self._decisions else self._default
        return ApprovalDecision(approved=allowed, reason="scripted test decision")
