"""Execution-memory scopes (DR-004).

DR-004 splits transient execution memory into three scopes with different
lifetimes, as distinct *types* rather than tagged entries in one store — so an
illegal cross-scope read is a structural mistake, not a convention someone
forgets.

* ``AgentMemory`` — one ``run()`` call. Live messages and tool-round
  bookkeeping. Discarded on return, *including* across a loopback
  re-invocation of the same stage, so a retry never inherits stale reasoning
  from the attempt that failed.
* ``RequestMemory`` — one user prompt. Canonical artifacts and the
  requirements ledger.
* ``SessionMemory`` — one interactive session. Bounded rolling history.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

__all__ = ["AgentMemory", "RequestMemory", "SessionMemory", "Exchange"]


class AgentMemory:
    """Private working memory for exactly one agent ``run()`` call.

    Nothing here is read by another agent or promoted wholesale into a wider
    scope: only the finished artifact the call returns moves upward, and the
    caller does that one field at a time.
    """

    __slots__ = ("messages", "round_count", "tool_call_count", "last_content")

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.round_count: int = 0
        self.tool_call_count: int = 0
        #: The most recent non-empty assistant content, kept so an exhausted
        #: round budget can surface a partial draft instead of nothing.
        self.last_content: str = ""

    def seed(
        self, system_prompt: str, session_context: Iterable[dict] | None = None
    ) -> None:
        """Start the message list: system prompt, then any session context.

        *session_context* is an ephemeral projection handed down from
        ``RequestMemory`` — rendering it here does not make this call's memory
        a second canonical copy of anything (DR-004, artifact ownership).
        """
        self.messages = [{"role": "system", "content": system_prompt}]
        if session_context:
            self.messages.extend(session_context)

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, message: dict) -> None:
        self.messages.append(message)
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            self.last_content = content

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )
        self.tool_call_count += 1

    def add_skipped_tool_result(self, tool_call_id: str, content: str) -> None:
        """Answer a call that was never dispatched, without counting it as work.

        A stop can land between two calls of one round (DR-030). The call it
        did not reach still needs a ``tool`` message — a provider rejects an
        assistant turn whose ``tool_calls`` went unanswered — but it did not
        gather anything, and ``tool_call_count`` is read as evidence gathered
        (DR-019) and reported to the user as work done.
        """
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def begin_round(self) -> int:
        self.round_count += 1
        return self.round_count


#: Largest a single canonical artifact may grow before it is a stage failure.
#: DR-004 forbids silently truncating one: an oversized design document is a
#: stage that did the wrong thing, and shrinking it hides that.
DEFAULT_MAX_ARTIFACT_CHARS = 60_000

#: Attempts summarised in the retry digest. Enough for a stage to see the shape
#: of what it already tried; bounded so a long loop cannot grow state.
_MAX_RETRY_DIGEST = 3

#: Characters of feedback kept per retry attempt.
_MAX_RETRY_FEEDBACK_CHARS = 400


class ArtifactTooLargeError(Exception):
    """A stage produced an artifact past its slot's cap (DR-004)."""


@dataclass(frozen=True)
class Attempt:
    """One prior try at a stage, in the bounded form a retry gets to see."""

    stage: str
    number: int
    feedback: str

    def render(self) -> str:
        return f"Attempt {self.number} at {self.stage}: {self.feedback}"


class RequestMemory:
    """Everything one user prompt needs, discarded when it completes (DR-004).

    Holds the canonical artifacts a multi-stage workflow hands between stages —
    the design document, the plan, the execution summary — plus the
    requirements ledger and a bounded digest of failed attempts.

    An artifact has exactly one owner: this object. Rendering one into a
    prompt is an ephemeral projection living in that call's ``AgentMemory``; it
    is never written back as a second canonical copy, which is what stops the
    same design document existing as both an artifact and a chat message.
    """

    def __init__(
        self,
        prompt: str,
        *,
        max_artifact_chars: int = DEFAULT_MAX_ARTIFACT_CHARS,
    ) -> None:
        self.prompt = prompt
        self._max_artifact_chars = max_artifact_chars
        self._artifacts: dict[str, str] = {}
        self._attempts: list[Attempt] = []
        #: The canonical ledger. Replaced wholesale by each stage that owns it.
        self.requirements: tuple = ()
        #: Files this request changed, in the order they were first touched.
        self._files_changed: list[str] = []

    # -- artifacts -------------------------------------------------------

    @property
    def artifacts(self) -> Mapping[str, str]:
        return dict(self._artifacts)

    def get(self, name: str, default: str = "") -> str:
        return self._artifacts.get(name, default)

    def has(self, name: str) -> bool:
        return name in self._artifacts

    def put(self, name: str, content: str) -> None:
        """Promote a finished artifact from a stage. Last write wins.

        Overwriting is correct: a retried stage supersedes its prior output
        entirely, and DR-004 promotes final state only. What the retry keeps
        instead of the discarded draft is the bounded digest below.
        """
        if len(content) > self._max_artifact_chars:
            raise ArtifactTooLargeError(
                f"the '{name}' artifact is {len(content)} characters, over the "
                f"{self._max_artifact_chars} limit for one stage output. Produce "
                "a more concise artifact rather than a longer one."
            )
        self._artifacts[name] = content

    # -- retry digest -----------------------------------------------------

    def record_attempt(self, stage: str, feedback: str) -> None:
        """Note that *stage* was sent back, keeping a short reason why."""
        number = sum(1 for item in self._attempts if item.stage == stage) + 1
        self._attempts.append(
            Attempt(
                stage=stage,
                number=number,
                feedback=" ".join((feedback or "").split())[:_MAX_RETRY_FEEDBACK_CHARS],
            )
        )
        del self._attempts[:-_MAX_RETRY_DIGEST]

    def attempts_for(self, stage: str) -> tuple[Attempt, ...]:
        return tuple(item for item in self._attempts if item.stage == stage)

    def retry_digest(self, stage: str) -> str:
        """What a retried stage is told about its own earlier attempts."""
        attempts = self.attempts_for(stage)
        if not attempts:
            return ""
        lines = "\n".join(item.render() for item in attempts)
        return (
            "You have already attempted this stage. Do not repeat these "
            f"approaches:\n{lines}"
        )

    # -- changed files ------------------------------------------------------

    def note_file_changed(self, path: str) -> None:
        if path not in self._files_changed:
            self._files_changed.append(path)

    @property
    def files_changed(self) -> tuple[str, ...]:
        return tuple(self._files_changed)


class SessionMemory:
    """Bounded rolling history for one interactive session (DR-004).

    Keeps the last few ``(prompt, answer)`` exchanges verbatim, plus one
    rolling summary of everything older. It never sees a raw tool-call
    transcript or a full per-stage artifact — only the distilled final pair a
    completed request promotes — which is what makes a tool transcript
    *structurally* unable to leak into long-term memory.

    When compression fails, the oldest exchange is dropped and the failure is
    reported rather than retried indefinitely: an unbounded retry on a failing
    summariser is how this scope would grow without limit.
    """

    #: Exchanges kept word for word before compression takes over.
    MAX_VERBATIM_EXCHANGES = 4

    #: Hard cap on the rolling summary. It is a fixed-size slot, not a
    #: second transcript.
    MAX_SUMMARY_CHARS = 2_000

    def __init__(
        self,
        *,
        max_verbatim: int | None = None,
        max_summary_chars: int | None = None,
        compress: Callable[[str, str], str] | None = None,
    ) -> None:
        self._max_verbatim = max_verbatim or self.MAX_VERBATIM_EXCHANGES
        self._max_summary_chars = max_summary_chars or self.MAX_SUMMARY_CHARS
        #: Strategy seam: how older history is condensed. ``None`` means no
        #: summariser is attached, and overflow is dropped rather than summarised.
        self._compress = compress
        self._exchanges: list[Exchange] = []
        self._summary = ""
        #: Exchanges lost because compression was unavailable or failed. Read
        #: by callers that want to tell the user history was dropped.
        self.dropped = 0
        self.compression_failures = 0

    @property
    def exchanges(self) -> tuple[Exchange, ...]:
        return tuple(self._exchanges)

    @property
    def summary(self) -> str:
        return self._summary

    def clear(self) -> None:
        self._exchanges.clear()
        self._summary = ""
        self.dropped = 0
        self.compression_failures = 0

    def restore(
        self, summary: str, exchanges: Iterable[tuple[str, str]]
    ) -> None:
        """Reinstate a stored session's history (DR-026).

        Replaces rather than appends, and re-bounds afterwards, so a record
        written when the verbatim cap was larger cannot smuggle a longer
        history past this session's configured bound.
        """
        self.clear()
        self._summary = (summary or "").strip()[: self._max_summary_chars]
        for prompt, answer in exchanges:
            self._exchanges.append(Exchange(prompt=prompt, answer=answer))
        # Drop, do not compress: these are already-summarised leftovers, and
        # paying for a compression call at resume time would make opening an
        # old session cost an inference request.
        while len(self._exchanges) > self._max_verbatim:
            self._exchanges.pop(0)
            self.dropped += 1

    def record(self, prompt: str, answer: str) -> None:
        """Promote one completed request's final pair, then re-bound."""
        self._exchanges.append(Exchange(prompt=prompt, answer=answer))
        while len(self._exchanges) > self._max_verbatim:
            self._absorb(self._exchanges.pop(0))

    def build_context(self) -> list[dict]:
        """Render history as messages for a stage prompt.

        An ephemeral projection: the caller passes this into one agent call and
        discards it. It is never a second canonical copy of anything.
        """
        messages: list[dict] = []
        if self._summary:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Summary of earlier conversation in this session:\n"
                        + self._summary
                    ),
                }
            )
        for exchange in self._exchanges:
            messages.append({"role": "user", "content": exchange.prompt})
            if exchange.answer:
                messages.append({"role": "assistant", "content": exchange.answer})
        return messages

    def _absorb(self, exchange: Exchange) -> None:
        """Fold the oldest exchange into the rolling summary, or drop it."""
        if self._compress is None:
            self.dropped += 1
            return
        try:
            summary = self._compress(self._summary, exchange.render())
        except Exception:  # noqa: BLE001 - a failing summariser must not retry forever
            self.compression_failures += 1
            self.dropped += 1
            return
        self._summary = (summary or "").strip()[: self._max_summary_chars]


@dataclass(frozen=True)
class Exchange:
    """One completed prompt/answer pair, the only thing promoted to a session."""

    prompt: str
    answer: str

    def render(self) -> str:
        return f"User: {self.prompt}\nAssistant: {self.answer}"
