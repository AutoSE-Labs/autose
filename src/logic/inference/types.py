"""Normalised request/response shapes for chat-completions inference.

Everything above this package works with the types here, never with a raw
provider payload. That is the DR-006 boundary in practice: a provider that
returns tool-call arguments as a JSON object instead of a JSON string, or
omits a call ``id``, is normalised at the edge, so no quirk leaks into the
agent loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ToolCall", "Usage", "CompletionResult", "StreamChunk"]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    #: Arguments parsed into a mapping. Empty when the provider sent something
    #: unparsable — :attr:`parse_error` then says what went wrong.
    arguments: dict[str, Any] = field(default_factory=dict)
    #: The arguments exactly as received, for audit and error messages.
    raw_arguments: str = ""
    parse_error: str = ""

    @classmethod
    def from_payload(cls, payload: dict, *, fallback_index: int = 0) -> ToolCall:
        function = payload.get("function") or {}
        raw = function.get("arguments", "")
        call_id = str(payload.get("id") or f"call_{fallback_index}")
        name = str(function.get("name") or "")

        if isinstance(raw, dict):
            # Some OpenAI-compatible servers send an object rather than the
            # spec's JSON string. Accept both.
            return cls(
                id=call_id,
                name=name,
                arguments=dict(raw),
                raw_arguments=json.dumps(raw, ensure_ascii=False),
            )
        raw_text = raw if isinstance(raw, str) else str(raw)
        if not raw_text.strip():
            return cls(id=call_id, name=name, arguments={}, raw_arguments=raw_text)
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return cls(
                id=call_id,
                name=name,
                raw_arguments=raw_text,
                parse_error=f"could not parse tool arguments as JSON: {exc}",
            )
        if not isinstance(parsed, dict):
            return cls(
                id=call_id,
                name=name,
                raw_arguments=raw_text,
                parse_error="tool arguments must be a JSON object",
            )
        return cls(id=call_id, name=name, arguments=parsed, raw_arguments=raw_text)

    def to_payload(self) -> dict:
        """Render back into the wire shape, for appending to a message list."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or "{}"},
        }


@dataclass(frozen=True)
class Usage:
    """Token accounting, when the provider reports it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def from_payload(cls, payload: dict | None) -> Usage:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class CompletionResult:
    """One assistant turn, normalised."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    #: Chain-of-thought a reasoning model emitted alongside its answer. Not
    #: part of the answer, and never replayed back to the model (DR-016).
    reasoning: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_empty(self) -> bool:
        """True when the turn produced neither an answer nor a tool call.

        For a reasoning model this usually means the output budget was spent
        thinking. DR-009 requires that be reported, not rendered as a blank
        success.
        """
        return not self.content.strip() and not self.tool_calls

    def to_message(self) -> dict:
        """The assistant message to append to the running history.

        Rebuilt from the normalised fields rather than echoing the provider's
        object, so a non-standard extra key cannot be replayed back at a
        different provider on a later round.
        """
        # `reasoning` is deliberately dropped: it is non-standard, providers
        # do not want it echoed, and replaying it would grow the context by
        # the model's entire thought process every round (DR-016).
        message: dict[str, Any] = {"role": "assistant"}
        message["content"] = self.content or None
        if self.tool_calls:
            message["tool_calls"] = [call.to_payload() for call in self.tool_calls]
        return message


@dataclass(frozen=True)
class StreamChunk:
    """One item from a streaming completion.

    Content arrives incrementally in :attr:`content`; the final chunk of a
    stream carries :attr:`result`, the fully assembled turn including any tool
    calls. Streaming and non-streaming rounds therefore produce the same
    :class:`CompletionResult`, so the agent loop has exactly one code path and
    a streaming round never costs a second generation to recover its tool
    calls.
    """

    content: str = ""
    #: Chain-of-thought delta, on its own channel so an Adapter can render it
    #: distinctly — or not at all — without it contaminating the answer.
    reasoning: str = ""
    result: CompletionResult | None = None

    @property
    def is_final(self) -> bool:
        return self.result is not None
