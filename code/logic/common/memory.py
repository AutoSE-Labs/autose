"""
Session memory manager for AutoSE.

Maintains a running conversation history across turns and compresses it
into a rolling summary when it grows too long, keeping the injected
context small while preserving useful session knowledge.

Typical usage (wired up by the TUI runners)::

    memory = MemoryManager()
    memory.set_call_fn(agent._call_sync)   # enables LLM-powered summarization

    # Before each agent call:
    context = memory.build_context_messages()
    agent.run(prompt, session_context=context)

    # After each completed turn:
    memory.add_exchange(user_prompt, full_assistant_response)
"""

from __future__ import annotations

from typing import Callable


class MemoryManager:
    """Cross-turn conversation memory with automatic LLM-powered summarization.

    History is stored as a list of ``{"user": str, "assistant": str}``
    exchange dicts.  Once the exchange count exceeds ``SUMMARY_THRESHOLD``,
    all but the most recent ``RECENT_KEEP`` exchanges are compressed into a
    rolling natural-language summary via the LLM, keeping the injected
    context window footprint small.
    """

    # Compress when the raw exchange list grows beyond this many entries.
    SUMMARY_THRESHOLD: int = 6
    # Always keep the last N exchanges verbatim (not compressed).
    RECENT_KEEP: int = 3

    _SUMMARY_SYSTEM = (
        "You are a concise technical assistant. "
        "Summarize conversation history into compact, factual prose."
    )
    _SUMMARY_USER = (
        "Summarize the following conversation into a brief paragraph that captures "
        "key goals, decisions, file paths, and outcomes. "
        "Preserve all important technical details but be as concise as possible.\n\n"
        "CONVERSATION:\n{history}"
    )

    def __init__(
        self,
        call_fn: Callable[[list[dict]], dict] | None = None,
    ) -> None:
        # Raw exchange history — cleared as entries are compressed.
        self._exchanges: list[dict] = []
        # Rolling natural-language summary of compressed exchanges.
        self._summary: str = ""
        # LLM callable: (messages: list[dict]) -> dict  (OpenAI-compatible response).
        self._call_fn = call_fn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_call_fn(self, call_fn: Callable[[list[dict]], dict]) -> None:
        """Wire up the LLM callable used for summarization.

        Safe to call multiple times — only the first non-None value is kept.
        """
        if self._call_fn is None:
            self._call_fn = call_fn

    def add_exchange(self, user: str, assistant: str) -> None:
        """Record a completed user→assistant turn.

        Triggers compression if the history has grown beyond the threshold
        and a summarization function is available.
        """
        self._exchanges.append({"user": user, "assistant": assistant})
        if self._call_fn and len(self._exchanges) > self.SUMMARY_THRESHOLD:
            self._compress()

    def build_context_messages(self) -> list[dict]:
        """Return a list of messages to inject *before* the current user prompt.

        The returned list is suitable for insertion between the system prompt
        and the current user message in any agent's message list::

            messages = [system_msg] + memory.build_context_messages() + [user_msg]

        Structure:
          - An optional system-role summary message (when a compressed
            summary exists).
          - Alternating user/assistant messages for recent raw exchanges.
        """
        messages: list[dict] = []
        if self._summary:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Context from earlier in this session:\n" + self._summary
                    ),
                }
            )
        for ex in self._exchanges:
            messages.append({"role": "user", "content": ex["user"]})
            messages.append({"role": "assistant", "content": ex["assistant"]})
        return messages

    def has_context(self) -> bool:
        """Return True if there is any prior session context to inject."""
        return bool(self._summary or self._exchanges)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compress(self) -> None:
        """Summarize all but the last ``RECENT_KEEP`` exchanges.

        The resulting summary is merged with any existing summary so that
        the entire session history is always captured in a single paragraph.
        """
        to_compress = self._exchanges[: -self.RECENT_KEEP]
        self._exchanges = self._exchanges[-self.RECENT_KEEP :]

        history_text = ""
        if self._summary:
            history_text += f"[Previous summary]\n{self._summary}\n\n"
        history_text += "[New exchanges]\n"
        for ex in to_compress:
            # Cap very long assistant replies so the summarization call stays cheap.
            preview = (
                ex["assistant"][:800] + "…"
                if len(ex["assistant"]) > 800
                else ex["assistant"]
            )
            history_text += f"User: {ex['user']}\nAssistant: {preview}\n\n"

        messages = [
            {"role": "system", "content": self._SUMMARY_SYSTEM},
            {
                "role": "user",
                "content": self._SUMMARY_USER.format(history=history_text),
            },
        ]
        try:
            response = self._call_fn(messages)
            self._summary = response["choices"][0]["message"]["content"].strip()
        except Exception:
            # Summarization failure is non-fatal — store plaintext as fallback.
            self._summary = history_text[:1200]
