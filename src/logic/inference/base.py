"""The ``ChatModel`` Strategy seam (DR-006).

Every model call in AutoSE — classification, agent rounds, knowledge
summarisation — goes through this interface. It describes an OpenAI-compatible
chat-completions exchange and nothing else: no vendor SDK, no capability that
only one provider offers. A new backend is a new implementation registered
under a name, never a branch inside calling code.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from .types import CompletionResult, StreamChunk

__all__ = ["ChatModel"]


@runtime_checkable
class ChatModel(Protocol):
    """An OpenAI-compatible chat-completions endpoint."""

    @property
    def model(self) -> str:
        """The model identifier sent with each request."""

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> CompletionResult:
        """Perform one non-streaming completion, used for tool-calling rounds.

        *reasoning_effort* is the standard OpenAI field (``"none"``, ``"low"``,
        ``"medium"``, ``"high"``); passed through as-is, and only when set, so
        a provider that ignores it (per DR-006, most do) is unaffected.
        """

    def stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[StreamChunk]:
        """Yield content deltas, ending with one chunk carrying the full result.

        The final chunk's ``result`` is equivalent to what :meth:`complete`
        would have returned for the same request — including tool calls — so a
        streaming round never needs a second generation to recover them.
        """
