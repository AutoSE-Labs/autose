"""The shared agent loop every stage is built on.

One class holds the machinery no stage should re-implement: the tool-calling
round loop, context budgeting (DR-010), the enforcement point for tool
permissions, approval and output bounding (DR-008/DR-011/DR-012), and the two
things that decide when a call ends — an *optional* round cap (unlimited by
default, DR-030) and the cancel token the person driving the run holds.

What a stage subclass supplies is only what actually differs: its system
prompt, and which tools it is entitled to. Everything else is inherited, which
is what makes "the validation stage has no edit tool" a property of one line
of configuration rather than of discipline.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import posixpath
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ..errors import MalformedToolCallError
from ..inference.base import ChatModel
from ..inference.types import CompletionResult, ToolCall, Usage
from ..limits import UNLIMITED, count_limit, counted, describe_limit
from .approval import ApprovalGate, ApprovalPolicy
from .cancel import CancelToken
from .context import ContextBudget, ContextMetrics
from .events import Event, EventSink, NullEventSink
from .execution import ToolExecutor
from .memory import AgentMemory
from .tool import Tool, ToolHandler, clone_tools, tools_schema
from .tool_output import BoundedToolOutput, FileToolOutputStore
from .toolcalls import parse_text_tool_call
from .tools import READ_ONLY_TOOL_NAMES
from .workspace import Workspace

__all__ = ["AgentResult", "AgentStream", "BaseAgent", "DEFAULT_MAX_TOOL_ROUNDS"]

#: Ceiling on tool-calling rounds inside one ``run()`` — unlimited by default
#: (DR-030). A small local model reads and searches in many more, smaller steps
#: than a frontier model does, so a fixed ceiling here stopped exactly the
#: models AutoSE targets partway through work they were doing correctly. What
#: ends a long call instead is the cancel token, which the person driving the
#: run holds. A repo that wants the old behaviour sets a number in its config.
#:
#: Distinct from DR-009's runner-level loopback cap either way: this bounds a
#: single agent call, that bounds how many times a workflow re-enters a stage.
DEFAULT_MAX_TOOL_ROUNDS = UNLIMITED

#: How many times a silent turn is nudged before the call gives up. Small
#: local models routinely finish gathering evidence and then stop without
#: writing the answer; asking for it costs one short round, whereas failing
#: the stage throws away everything they just read (DR-019).
DEFAULT_MAX_EMPTY_TURN_NUDGES = 2

#: Below this share of the estimated input, a provider's reported
#: ``prompt_tokens`` is treated as evidence the runtime silently dropped part
#: of the request rather than as tokenizer drift (DR-022).
#:
#: The local estimate deliberately runs high (three characters per token
#: against a real ~four), so a modest shortfall is expected and must not warn.
#: Losing half the request is not drift — it is a window smaller than the one
#: the runtime advertised, and the user cannot see it any other way: the
#: request is not rejected, it is truncated from the front, taking the system
#: prompt with it.
TRUNCATION_WARNING_RATIO = 0.5

#: Two nudges, because a silence before any tool call and a silence after one
#: need opposite advice. Telling a stage that has gathered nothing to stop
#: using tools is worse than saying nothing at all.
_NUDGE_AFTER_TOOLS = (
    "You stopped without producing an answer. You have already gathered what "
    "you need — do not call any more tools. Write your complete final response "
    "now, following the required format exactly."
)
#: Retries of a round whose tool call the runtime could not parse (DR-039).
#: A model that writes one malformed call usually writes the next correctly;
#: one that keeps failing is not going to be talked out of it.
_MAX_MALFORMED_TOOL_CALLS = 3

_MALFORMED_TOOL_CALL_NUDGE = (
    "Your last reply contained a tool call the runtime could not parse: "
    "{error}. Write the tool call again with every parameter given exactly "
    "once and correctly closed, or answer without a tool call."
)

_NUDGE_BEFORE_TOOLS = (
    "You produced no response. Start now: use your tools to read whatever you "
    "need from the workspace, then write your complete final response in the "
    "required format."
)

#: How many times one read-only tool may be called with the *exact same*
#: arguments in one run before a repeat is refused rather than re-run. A small
#: local model that does not converge re-issues the identical call instead of
#: acting on what it already has — most visibly a `list_files`/`read_file` pair
#: repeated for dozens of rounds against a result that never changes, which
#: burns the wait DR-030 otherwise spends on genuine (if plentiful) small
#: steps. Two is generous: a second look at the same thing is routine, a third
#: is not gathering, it is stalling.
_MAX_IDENTICAL_READ_ONLY_CALLS = 2

#: How many rounds in a row may teach the model nothing new before the call
#: is ended as stalled. DR-030 removed the fixed round cap on purpose — a
#: small local model doing genuine work in many small steps must not be cut
#: off partway — so what ends a runaway call is the *absence of progress*,
#: not the length of the run.
#:
#: This is the backstop `_MAX_IDENTICAL_READ_ONLY_CALLS` cannot be: that tally
#: keys on arguments, and a model that is stalling varies its arguments
#: without varying the answer (`list_files(".")`, then `list_files("./")`).
#: Keying on the result instead makes two spellings of one question count
#: once. Three rounds, because a single refused repeat is a recoverable
#: stumble and a model that has learned nothing across three consecutive
#: rounds is not about to.
_MAX_STALLED_ROUNDS = 3

#: Argument names that hold a workspace path, and so may be normalised before
#: two calls are compared. Deliberately not `pattern`, `text`, `file_pattern`
#: or `name_pattern`: those are globs and search strings, where `a//b` and
#: `a/b` are genuinely different requests and collapsing them would refuse a
#: call the model had every reason to make.
_PATH_ARGUMENT_NAMES = frozenset({"path", "directory"})

_STALLED_NOTICE = (
    "{name} stopped after {rounds} rounds in a row that learned nothing new — "
    "it kept re-reading what it had already been given instead of answering"
)

#: Appended when the budget was evicting history during *some* of those rounds
#: but not all of them. The model started looping while it still had what it
#: had read, so the window is not the verdict — but it is short enough to be
#: making things worse, and saying nothing about it sends the user to change
#: the one thing that was not the whole problem.
_STALLED_WITH_EVICTION = (
    ". The context window was also too small to keep every round's evidence "
    "({evicting} of those {rounds} rounds had to discard earlier results), so "
    "raising it may help as well"
)

#: The same symptom, the opposite cause — and so the opposite advice.
#:
#: When the budget is evicting history on the very rounds that learned
#: nothing, the model did not ignore what it had been given: it no longer had
#: it. Each round dropped the previous round's tool results to fit the window,
#: so the model re-read the file it could no longer see, and the no-progress
#: guard read that as going in circles. Blaming the model here sends the user
#: to swap it, which does not help; the window is what is short.
_CONTEXT_THRASHING_NOTICE = (
    "{name} stopped after {rounds} rounds that learned nothing new, because "
    "each round dropped the evidence gathered by the one before it to fit a "
    "{limit:,}-token context window ({budget:,} tokens for messages, after "
    "tool schemas and reserved output). The model re-read what it could no "
    "longer see. Raise the served context window — this is not a model that "
    "is going in circles, it is one being made to forget."
)

_REPEATED_CALL_NOTICE = (
    "Refused: you already called {tool}({args}) {count} times this turn and "
    "the result cannot have changed — you have not written to the workspace "
    "since. Use what that call already returned. Do not call it again with the "
    "same arguments; either call it with different arguments, or write your "
    "answer now."
)


def _window_bounded_handlers(
    tools: Mapping[str, Tool],
    caller_overrides: Mapping[str, ToolHandler] | None,
    allowance: int | None,
) -> dict[str, ToolHandler]:
    """Bind *allowance* to every handler that takes a ``max_chars`` bound.

    A tool that can return an unbounded amount of text — a file read, a page
    fetch — accepts a character bound so it can stop at a clean boundary and
    say where it stopped. Left at its own default, that bound is chosen with
    no knowledge of the window, and an oversized result is then truncated a
    *second* time by `BoundedToolOutput`, leaving the model two truncation
    notices naming two different recovery routes for one call.

    Bound as a keyword, so a tool that publishes ``max_chars`` in its schema
    still lets an explicit model-supplied value win: this sets the default,
    it does not overrule the request.
    """
    if allowance is None:
        return {}
    overridden = set(caller_overrides or {})
    bound: dict[str, ToolHandler] = {}
    for name, tool in tools.items():
        if name in overridden:
            # The caller configured this handler deliberately; a bound applied
            # on top would silently re-open a decision they already made.
            continue
        try:
            parameter = inspect.signature(tool.fn).parameters.get("max_chars")
        except (TypeError, ValueError):  # a builtin or C handler
            continue
        if parameter is None:
            continue
        bound[name] = partial(tool.fn, max_chars=allowance)
    return bound


class _RoundCancelled(Exception):
    """Internal signal: a stop landed mid-stream, before the round finished.

    Never escapes :meth:`BaseAgent._iterate` — it exists only to unwind
    ``_stream_round`` immediately instead of waiting for the model to finish
    the round it is mid-way through generating (DR-030).
    """


@dataclass(frozen=True)
class AgentResult:
    """The outcome of one agent call."""

    content: str
    #: ``"answered"`` when the model finished on its own; ``"round_cap"`` when
    #: a configured tool-round ceiling stopped it and *content* is a partial
    #: draft; ``"cancelled"`` when the user asked it to stop, likewise partial;
    #: ``"empty_answer"`` when it produced neither an answer nor a tool call.
    stop_reason: str = "answered"
    rounds: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    context_metrics: tuple[ContextMetrics, ...] = ()

    @property
    def complete(self) -> bool:
        return self.stop_reason == "answered"


class AgentStream:
    """An in-progress streaming run: iterate for text, then read ``result``.

    Adapters that render token-by-token consume the iterator; the finished
    :class:`AgentResult` is available afterwards without a second call.
    """

    def __init__(self, chunks: Iterator[str], holder: dict[str, AgentResult]) -> None:
        self._chunks = chunks
        self._holder = holder

    def __iter__(self) -> Iterator[str]:
        yield from self._chunks

    @property
    def result(self) -> AgentResult | None:
        """The completed result, or ``None`` while the stream is unfinished."""
        return self._holder.get("result")

    def collect(self) -> AgentResult:
        """Drain the stream, discarding the text, and return the result."""
        for _ in self._chunks:
            pass
        result = self._holder.get("result")
        assert result is not None, "stream finished without producing a result"
        return result


class BaseAgent:
    """Runs one bounded tool-calling conversation against a ``ChatModel``."""

    #: Human-readable stage name, used in emitted events.
    name: str = "agent"

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Mapping[str, Tool],
        workspace: Workspace,
        budget: ContextBudget | None = None,
        approval_gate: ApprovalGate | None = None,
        approval_policy: ApprovalPolicy | None = None,
        events: EventSink | None = None,
        tool_overrides: Mapping[str, ToolHandler] | None = None,
        bounded_output: BoundedToolOutput | None = None,
        max_tool_rounds: int | None = DEFAULT_MAX_TOOL_ROUNDS,
        max_empty_turn_nudges: int | None = DEFAULT_MAX_EMPTY_TURN_NUDGES,
        cancel: CancelToken | None = None,
        name: str | None = None,
    ) -> None:
        if name:
            self.name = name
        self._model = model
        self._workspace = workspace
        self._events = events or NullEventSink()
        self._budget = budget or ContextBudget()
        # ``None`` past this point means "no limit" for both, which is what the
        # loop below branches on (DR-030); a configured number arrives here as
        # ``-1`` or as a count, and `count_limit` resolves which it is.
        self._max_tool_rounds = count_limit(max_tool_rounds)
        self._max_empty_turn_nudges = count_limit(max_empty_turn_nudges, minimum=0)
        # A token of its own when the caller supplied none, so every check
        # below is unconditional: there is no "cancellable" and "plain" loop.
        self._cancel = cancel if cancel is not None else CancelToken()

        # DR-008: this instance owns its registry. A runner instrumenting a
        # handler patches the clone, which dies with the agent — no shared
        # global to race on, and no restoration step to forget.
        # DR-008: this instance owns its registry, so the window-derived
        # bounds below are applied to the clone and never to the catalog.
        self._tools: dict[str, Tool] = clone_tools(tools, tool_overrides)
        self._tools_schema: list[dict] = tools_schema(self._tools)

        # How much of one tool result this window can actually carry between
        # rounds. Both bounds below are read against it, because a bound wider
        # than the budget does not limit the context — it guarantees the next
        # round evicts what this one gathered, and the agent re-reads it.
        output = bounded_output or BoundedToolOutput(FileToolOutputStore(workspace))
        allowance = self._budget.tool_result_char_allowance(
            self._tools_schema, configured=output.max_chars
        )
        self._tools = clone_tools(
            self._tools, _window_bounded_handlers(self._tools, tool_overrides, allowance)
        )
        self._executor = ToolExecutor(
            self._tools,
            workspace=workspace,
            approval_gate=approval_gate,
            approval_policy=approval_policy,
            events=self._events,
            bounded_output=output.clamped(allowance),
        )

    # -- subclass surface ------------------------------------------------

    def system_prompt(self) -> str:
        """Return this stage's system prompt. Subclasses must override."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement system_prompt()"
        )

    # -- introspection ---------------------------------------------------

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    @property
    def cancel_token(self) -> CancelToken:
        """The stop signal this call watches (DR-030)."""
        return self._cancel

    @property
    def max_tool_rounds(self) -> int | None:
        """The round ceiling in force, or ``None`` when there is none."""
        return self._max_tool_rounds

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def instrument(self, name: str, wrap: Callable[[ToolHandler], ToolHandler]) -> None:
        """Wrap one tool's handler on *this instance's* registry.

        The DR-008-sanctioned way to observe a tool: the clone is this agent's
        own, so there is no shared handler to race on and nothing to restore —
        the wrapper dies with the agent. Composing rather than replacing is
        what lets an observer (which files changed) layer over a restriction
        (where this stage may write) without either one erasing the other.
        """
        tool = self._tools.get(name)
        if tool is None:
            return
        tool.fn = wrap(tool.fn)

    # -- execution --------------------------------------------------------

    def run(
        self, prompt: str, *, session_context: Sequence[dict] | None = None
    ) -> AgentResult:
        """Run the loop to completion and return the final answer."""
        holder: dict[str, AgentResult] = {}
        for _ in self._iterate(prompt, session_context, holder, stream=False):
            pass
        return holder["result"]

    def stream(
        self, prompt: str, *, session_context: Sequence[dict] | None = None
    ) -> AgentStream:
        """Run the loop, yielding content as the model produces it."""
        holder: dict[str, AgentResult] = {}
        return AgentStream(
            self._iterate(prompt, session_context, holder, stream=True), holder
        )

    # -- the loop ----------------------------------------------------------

    def _iterate(
        self,
        prompt: str,
        session_context: Sequence[dict] | None,
        holder: dict[str, AgentResult],
        *,
        stream: bool,
    ) -> Generator[str, None, None]:
        memory = AgentMemory()
        memory.seed(self.system_prompt(), session_context)
        memory.add_user(prompt)

        usage = Usage()
        metrics: list[ContextMetrics] = []
        nudges = 0
        # Warn once per call: the condition persists across rounds, and
        # repeating it every round buries the round that revealed it.
        truncation_reported = False
        # How many times each (read-only tool, arguments) pair has been called
        # this run — cleared whenever a non-read-only call runs, since a write
        # or a shell command can change what a subsequent read returns.
        repeat_counts: dict[tuple[str, str], int] = {}
        # Consecutive read_file calls whose range is a single line. A model
        # that walks a file one line at a time never repeats an argument, so
        # the identical-call guard does not see it.
        one_line_streak = 0
        # Digests of the results already handed back, and how many consecutive
        # rounds have added nothing to that set. Cleared together with
        # `repeat_counts` whenever the workspace may have changed under them.
        seen_results: set[str] = set()
        stalled_rounds = 0
        # How many of the current no-progress rounds were also rounds the
        # budget had to drop history from. When the two counts agree, the
        # "learned nothing new" the guard measured is eviction, not a loop —
        # a different diagnosis, and a different fix.
        evicting_rounds = 0
        last_metrics: ContextMetrics | None = None
        malformed_calls = 0

        for _ in counted(self._max_tool_rounds):
            if self._cancel.requested:
                # Checked before the round rather than after it, so a stop
                # asked for while a round was running costs no further
                # inference — and whatever the model had already written is
                # returned instead of discarded (DR-030).
                holder["result"] = self._cancelled(memory, usage, metrics)
                return

            round_number = memory.begin_round()
            prepared = self._budget.prepare(memory.messages, self._tools_schema)
            if prepared.metrics is not None:
                metrics.append(prepared.metrics)
                last_metrics = prepared.metrics
                if prepared.metrics.dropped_message_count:
                    evicting_rounds += 1

            try:
                if stream:
                    try:
                        completion = yield from self._stream_round(prepared.messages)
                    except _RoundCancelled:
                        # The round itself is abandoned, not folded into
                        # history: `memory` has not seen this round's
                        # (incomplete) content, so the cancelled result falls
                        # back to the last complete round instead of a sentence
                        # cut off mid-word (DR-030).
                        holder["result"] = self._cancelled(memory, usage, metrics)
                        return
                else:
                    completion = self._model.complete(
                        prepared.messages,
                        tools=self._tools_schema,
                        max_output_tokens=self._budget.max_output_tokens,
                    )
            except MalformedToolCallError as exc:
                # DR-039: one bad generation, not a broken connection. Say what
                # was wrong and ask again in this call, keeping what the stage
                # has gathered; only a model that keeps failing ends the stage.
                if malformed_calls >= _MAX_MALFORMED_TOOL_CALLS:
                    raise
                malformed_calls += 1
                detail = " ".join(str(exc).split())[:240]
                memory.add_user(_MALFORMED_TOOL_CALL_NUDGE.format(error=detail))
                self._emit(
                    "tool_call_malformed",
                    detail,
                    stage=self.name,
                    round=round_number,
                    attempt=malformed_calls,
                )
                continue
            usage = _add_usage(usage, completion.usage)
            # Per-round usage, so an adapter can show tokens and context fill
            # while a stage is still running rather than only when it ends
            # (DR-033). The estimate is the budget's own figure, which is the
            # only one available from a runtime that reports no usage at all.
            self._emit(
                "round_usage",
                stage=self.name,
                round=round_number,
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                estimated_input_tokens=(
                    prepared.metrics.estimated_input_tokens
                    if prepared.metrics is not None
                    else 0
                ),
                context_limit=(
                    prepared.metrics.context_limit
                    if prepared.metrics is not None
                    else None
                ),
            )

            if not truncation_reported and _looks_truncated(
                prepared.metrics, completion.usage
            ):
                truncation_reported = True
                # `_looks_truncated` returns False for absent metrics, so
                # reaching here proves they exist. Bound to a local so that is
                # a fact the reader and the type checker share.
                metrics_now = prepared.metrics
                assert metrics_now is not None
                self._emit(
                    "context_truncated_by_runtime",
                    (
                        "the runtime processed far fewer input tokens than were "
                        "sent — it is serving a smaller context window than it "
                        "reported, and silently dropped the earliest messages "
                        "(which include the system prompt)"
                    ),
                    estimated_input_tokens=metrics_now.estimated_input_tokens,
                    reported_prompt_tokens=completion.usage.prompt_tokens,
                    context_limit=metrics_now.context_limit,
                )

            if completion.reasoning and not stream:
                # Chain of thought is telemetry, not the answer: it reaches
                # Adapters as an event so they may render it distinctly, and
                # it never joins the answer text or the message history
                # (DR-016).
                self._emit(
                    "agent_reasoning",
                    completion.reasoning,
                    stage=self.name,
                    round=round_number,
                )

            completion = self._recover_text_tool_call(completion)
            if not completion.has_tool_calls:
                if (
                    completion.is_empty
                    and not memory.last_content
                    and (
                        # `None` is DR-030's "no limit"; comparing against it
                        # would raise rather than nudge forever.
                        self._max_empty_turn_nudges is None
                        or nudges < self._max_empty_turn_nudges
                    )
                ):
                    # The model gathered evidence and then went quiet. Ask for
                    # the answer *in this call*, so the tool results it just
                    # collected are still in context — restarting the stage
                    # would discard them and repeat the same dead end (DR-019).
                    nudges += 1
                    gathered = memory.tool_call_count > 0
                    memory.add_user(
                        _NUDGE_AFTER_TOOLS if gathered else _NUDGE_BEFORE_TOOLS
                    )
                    self._emit(
                        "agent_nudged",
                        "asked to continue" if not gathered else "asked for the final answer",
                        stage=self.name,
                        attempt=nudges,
                        gathered_evidence=gathered,
                        finish_reason=completion.finish_reason,
                    )
                    continue

                holder["result"] = AgentResult(
                    content=completion.content or memory.last_content,
                    stop_reason=self._final_stop_reason(completion, memory),
                    rounds=round_number,
                    tool_calls=memory.tool_call_count,
                    usage=usage,
                    context_metrics=tuple(metrics),
                )
                return

            memory.add_assistant(completion.to_message())
            # Whether this round taught the model anything it did not already
            # have. A round of refused repeats and re-reads leaves it False.
            progressed = False
            for call in completion.tool_calls:
                if self._cancel.requested:
                    # Stop between calls, never mid-call. The ones not reached
                    # are answered but not counted: the transcript stays
                    # well-formed (an assistant turn whose `tool_calls` went
                    # unanswered is a request most providers reject), while the
                    # tally still reports only what actually ran.
                    memory.add_skipped_tool_result(
                        call.id, "Not run — you asked AutoSE to stop."
                    )
                    continue
                if call.name not in READ_ONLY_TOOL_NAMES:
                    # State may just have changed under whatever a read-only
                    # call returned earlier, so neither its repeat count nor
                    # its result is evidence of stalling any more.
                    repeat_counts.clear()
                    seen_results.clear()
                    one_line_streak = 0
                    result = self._executor.execute(call)
                    memory.add_tool_result(call.id, result.content)
                    progressed = True
                    continue
                repeated = self._check_repeated_call(call, repeat_counts)
                if repeated is not None:
                    memory.add_tool_result(call.id, repeated)
                    continue
                if self._is_one_line_read(call):
                    one_line_streak += 1
                    if one_line_streak > 3:
                        memory.add_tool_result(
                            call.id,
                            "Refused: do not read a file one line at a time. "
                            "Request a range of at least 50 lines, or write "
                            "your answer now.",
                        )
                        continue
                elif call.name == "read_file":
                    one_line_streak = 0
                result = self._executor.execute(call)
                memory.add_tool_result(call.id, result.content)
                if self._is_new_evidence(call, result.content, seen_results):
                    progressed = True

            if progressed:
                stalled_rounds = 0
                evicting_rounds = 0
                continue
            stalled_rounds += 1
            if stalled_rounds < _MAX_STALLED_ROUNDS:
                continue
            # Unlimited rounds (DR-030) means nothing else would ever end this
            # call: the model is answering every refusal with another call the
            # refusal already covered. Surface what it has rather than spend
            # the user's machine on a round that cannot differ from the last.
            thrashing = (
                evicting_rounds >= stalled_rounds
                and last_metrics is not None
                and last_metrics.context_limit is not None
            )
            if thrashing:
                assert last_metrics is not None  # narrowed by `thrashing`
                self._emit(
                    "agent_context_thrashing",
                    _CONTEXT_THRASHING_NOTICE.format(
                        name=self.name,
                        rounds=stalled_rounds,
                        limit=last_metrics.context_limit or 0,
                        budget=last_metrics.message_budget or 0,
                    ),
                    stage=self.name,
                    round=round_number,
                    stalled_rounds=stalled_rounds,
                    context_limit=last_metrics.context_limit,
                    message_budget=last_metrics.message_budget,
                    estimated_tool_schema_tokens=(
                        last_metrics.estimated_tool_schema_tokens
                    ),
                    reserved_output_tokens=last_metrics.reserved_output_tokens,
                )
            else:
                notice = _STALLED_NOTICE.format(
                    name=self.name, rounds=stalled_rounds
                )
                if evicting_rounds:
                    notice += _STALLED_WITH_EVICTION.format(
                        evicting=evicting_rounds, rounds=stalled_rounds
                    )
                self._emit(
                    "agent_stalled",
                    notice,
                    stage=self.name,
                    round=round_number,
                    stalled_rounds=stalled_rounds,
                    evicting_rounds=evicting_rounds,
                )
            holder["result"] = AgentResult(
                content=memory.last_content,
                stop_reason="context_thrashing" if thrashing else "stalled",
                rounds=round_number,
                tool_calls=memory.tool_call_count,
                usage=usage,
                context_metrics=tuple(metrics),
            )
            return

        # A configured round budget ran out — unreachable when rounds are
        # unlimited, which is the default (DR-030). Surface whatever the model
        # has already drafted rather than paying for another round it is
        # unlikely to finish, and say plainly it is not a completed answer.
        self._emit(
            "agent_round_cap_reached",
            f"{self.name} stopped after "
            f"{describe_limit(self._max_tool_rounds)} tool rounds",
            stage=self.name,
            max_tool_rounds=self._max_tool_rounds,
        )
        holder["result"] = AgentResult(
            content=memory.last_content,
            stop_reason="round_cap",
            rounds=memory.round_count,
            tool_calls=memory.tool_call_count,
            usage=usage,
            context_metrics=tuple(metrics),
        )

    def _cancelled(
        self,
        memory: AgentMemory,
        usage: Usage,
        metrics: list[ContextMetrics],
    ) -> AgentResult:
        """The partial result of a call the user stopped (DR-030)."""
        self._emit(
            "agent_cancelled",
            self._cancel.reason or f"{self.name} stopped at your request",
            stage=self.name,
            reason=self._cancel.reason,
            rounds=memory.round_count,
            tool_calls=memory.tool_call_count,
        )
        return AgentResult(
            content=memory.last_content,
            stop_reason="cancelled",
            rounds=memory.round_count,
            tool_calls=memory.tool_call_count,
            usage=usage,
            context_metrics=tuple(metrics),
        )

    def _check_repeated_call(
        self, call: ToolCall, repeat_counts: dict[tuple[str, str], int]
    ) -> str | None:
        """Refuse a read-only call repeated past DR-030's stalling threshold.

        Returns the tool-result text to hand back in place of running the
        call again, or ``None`` when this call should run as normal.
        """
        key = self._repeat_key(call)
        if key is None:
            # Arguments that cannot be serialised deterministically cannot be
            # compared for repetition either — let the call through.
            return None
        count = repeat_counts.get(key, 0) + 1
        repeat_counts[key] = count
        if count <= _MAX_IDENTICAL_READ_ONLY_CALLS:
            return None
        args = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
        self._emit(
            "tool_call_repeated",
            f"refused a repeat of {call.name}({args})",
            stage=self.name,
            tool=call.name,
            arguments=dict(call.arguments),
            count=count,
        )
        return _REPEATED_CALL_NOTICE.format(tool=call.name, args=args, count=count)

    @staticmethod
    def _repeat_key(call: ToolCall) -> tuple[str, str] | None:
        """A key under which two spellings of one call compare equal.

        Path arguments are normalised first, because they are the ones a model
        varies without meaning anything by it: `"."`, `"./"`, `""` and `"./."`
        are all the workspace root, and a tally that treats them as four
        different calls never reaches its threshold.
        """
        try:
            normalised = {
                name: _normalise_path(value)
                if name in _PATH_ARGUMENT_NAMES and isinstance(value, str)
                else value
                for name, value in call.arguments.items()
            }
            return (call.name, json.dumps(normalised, sort_keys=True, default=str))
        except TypeError:
            return None

    @staticmethod
    def _is_one_line_read(call: ToolCall) -> bool:
        """True when read_file asks for exactly one line."""
        if call.name != "read_file":
            return False
        start = call.arguments.get("start_line")
        end = call.arguments.get("end_line")
        if start is None or end is None:
            return False
        try:
            return int(end) == int(start)
        except (TypeError, ValueError):
            return False

    def _is_new_evidence(self, call: ToolCall, content: str, seen: set[str]) -> bool:
        """Whether this result told the model something it did not already have.

        Digested rather than stored whole: a run may read a great many large
        files, and this set lives for the whole call.
        """
        digest = hashlib.sha256(
            f"{call.name}\x00{content}".encode(errors="replace")
        ).hexdigest()
        if digest in seen:
            return False
        seen.add(digest)
        return True

    @staticmethod
    def _final_stop_reason(completion: CompletionResult, memory: AgentMemory) -> str:
        """Classify a turn that made no tool call.

        A reasoning model that spends its whole output budget thinking returns
        empty content and no tool call. That is not an answer, and DR-009
        forbids presenting it as one — so it gets its own stop reason rather
        than being reported as a successful blank response.
        """
        if completion.is_empty and not memory.last_content:
            # Distinguish "ran out of room to answer" from "chose to say
            # nothing": they call for different advice to the user.
            return (
                "output_budget_exhausted"
                if completion.finish_reason == "length"
                else "empty_answer"
            )
        return "answered"

    def _stream_round(
        self, messages: list[dict]
    ) -> Generator[str, None, CompletionResult]:
        """Forward content deltas, returning the assembled completion.

        Reasoning deltas are consumed here and re-emitted as events rather
        than yielded, so an Adapter iterating this stream receives the answer
        and only the answer.

        Checks the cancel token between chunks rather than only between
        rounds: a round can be the model generating for minutes, and a stop
        that had to wait for that to finish is the opposite of a stop
        (DR-030). The underlying stream is closed explicitly rather than left
        for garbage collection, so the connection — and the runtime still
        generating on the other end of it — is actually released now.
        """
        completion: CompletionResult | None = None
        chunks = self._model.stream(
            messages,
            tools=self._tools_schema,
            max_output_tokens=self._budget.max_output_tokens,
        )
        try:
            for chunk in chunks:
                if self._cancel.requested:
                    raise _RoundCancelled()
                if chunk.result is not None:
                    completion = chunk.result
                    continue
                if chunk.reasoning:
                    self._emit(
                        "agent_reasoning_delta", chunk.reasoning, stage=self.name
                    )
                if chunk.content:
                    yield chunk.content
        finally:
            # `ChatModel.stream` is declared as `Iterator`, not `Generator`
            # (DR-006 — a provider need not implement one to satisfy the
            # other), so a `close()` is a courtesy some, not all, provide.
            close = getattr(chunks, "close", None)
            if close is not None:
                close()
        if completion is None:
            # A provider that ended the stream without a terminal chunk still
            # has to produce *something* the loop can act on.
            completion = CompletionResult()
        return completion

    def _recover_text_tool_call(self, completion: CompletionResult) -> CompletionResult:
        """Rescue a tool call the model wrote as prose instead of structured output."""
        if completion.has_tool_calls or not completion.content:
            return completion
        recovered = parse_text_tool_call(completion.content, self._tools)
        if recovered is None:
            return completion
        return CompletionResult(
            content="",
            tool_calls=(recovered,),
            usage=completion.usage,
            finish_reason=completion.finish_reason,
        )

    def _emit(self, event_type: str, message: str = "", **data: Any) -> None:
        self._events.emit(Event(type=event_type, message=message, data=data))


def _normalise_path(value: str) -> str:
    """Collapse the interchangeable spellings of one workspace path.

    Only differences that cannot change which file is meant are collapsed:
    surrounding whitespace, backslash separators, a redundant `./`, and a
    trailing slash. The empty string and `"."` both name the root.
    """
    text = value.strip().replace("\\", "/")
    if not text:
        return "."
    return posixpath.normpath(text) or "."


def _looks_truncated(metrics: ContextMetrics | None, usage: Usage) -> bool:
    """Whether the provider's own token count says the request was cut down.

    Only a provider that reports usage can be checked, and only a round that
    actually sent something is meaningful — hence both guards before the
    ratio. Returning False on missing data is deliberate: an unknown is not
    evidence of truncation (DR-022).
    """
    if metrics is None or usage.prompt_tokens <= 0:
        return False
    estimated = metrics.estimated_input_tokens
    if estimated <= 0:
        return False
    return usage.prompt_tokens < estimated * TRUNCATION_WARNING_RATIO


def _add_usage(total: Usage, addition: Usage) -> Usage:
    return Usage(
        prompt_tokens=total.prompt_tokens + addition.prompt_tokens,
        completion_tokens=total.completion_tokens + addition.completion_tokens,
    )
