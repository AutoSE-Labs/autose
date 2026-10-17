"""The multi-stage runner behind `craft` (DR-003, DR-005, DR-009).

`craft` is Plan → Execute → Validate. The runner takes its stage list as a
parameter rather than hard-coding it, so a tier with a different shape is a
different list given to the same runner, not a second pipeline that will drift.

Three rules the runner owns, which no stage is trusted to self-enforce:

* **The ledger is the contract** (DR-005). Every stage that touches
  requirements is re-parsed by the runner, must echo back exactly the IDs it
  was given, and a claimed pass with anything unverified is overridden into a
  loopback naming the specific unmet IDs.
* **The loop is bounded when a repo asks for it** (DR-009, amended by
  DR-030). A wall-clock budget and a loopback cap, both checked here, both
  raised as typed failures, and both unlimited by default — the runner's job
  is to enforce whatever bound it was given and to hold the stop signal the
  user can pull, not to guess a number that suits every model.
* **A malformed stage output is a stage failure**, retried with the parser's
  own complaint as feedback rather than failed outright. A small local model
  gets a format wrong sometimes, and handing it the exact error is far cheaper
  than failing the request — so by default it is retried for as long as the
  user lets it (DR-030), and a configured `max_format_retries` turns that back
  into a bounded loop that gives up.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass

from ..common.agent import AgentResult
from ..common.cancel import CancelToken
from ..common.changes import ChangeJournal, track_changes
from ..common.context import ContextBudget
from ..common.events import Event, EventSink
from ..common.memory import ArtifactTooLargeError, RequestMemory
from ..common.tool_output import BoundedToolOutput, FileToolOutputStore
from ..errors import (
    ContextBudgetError,
    IterationBudgetExhausted,
    RunCancelled,
    StageExecutionError,
    TimeBudgetExhausted,
)
from ..limits import (
    UNLIMITED,
    count_limit,
    counted,
    describe_limit,
    duration_limit,
)
from ..prompt_overrides import resolve_prompt
from ..requirements import (
    Requirement,
    RequirementsFormatError,
    format_requirements,
    parse_requirements,
    require_same_ids,
    strip_requirements_section,
    summarise,
)
from ..tuning import Tuning
from .base import WorkflowOutcome, WorkflowRequest
from .stages import EXECUTE, PLAN, STAGES, VALIDATE, StageAgent, StageSpec
from .verdict import Verdict, derive_verdict

__all__ = ["StagedWorkflow", "WorkflowBounds", "DEFAULT_BOUNDS"]


@dataclass(frozen=True)
class WorkflowBounds:
    """The two independent bounds DR-009 requires, plus stage-retry limits.

    Both bounds are needed when either is used, not one: runtime varies
    enormously by model and hardware, so a time budget alone cannot express how
    many failed recovery attempts are reasonable — and an iteration cap alone
    cannot stop a single stage from running for an hour on slow hardware.

    Every field is ``-1`` — unlimited — by default (DR-030). The normalised
    properties below, not the raw fields, are what the runner branches on; the
    raw values stay as configured so an event or a message can report what a
    repo actually asked for.
    """

    #: How many validation rounds are allowed. The last one cannot send work
    #: back — reaching this count stops the workflow — so a cap of 3 permits
    #: two loopbacks and three attempts at passing.
    max_validation_iterations: int = UNLIMITED
    #: Wall-clock ceiling for the whole request.
    time_budget_seconds: float = float(UNLIMITED)
    #: Re-attempts of one stage whose output failed to parse.
    max_format_retries: int = UNLIMITED
    #: Tool-calling rounds inside a single stage call.
    max_tool_rounds: int = UNLIMITED

    @property
    def validation_iterations(self) -> int | None:
        """The loopback cap as a count, or ``None`` for unlimited."""
        return count_limit(self.max_validation_iterations)

    @property
    def time_budget(self) -> float | None:
        """The wall-clock budget in seconds, or ``None`` for unlimited."""
        return duration_limit(self.time_budget_seconds)

    @property
    def stage_attempts(self) -> int | None:
        """Total attempts allowed at one stage — retries *plus* the first try.

        ``None`` for unlimited. Doing the ``+ 1`` here rather than at the call
        site is what keeps "2 retries" from being read as "2 attempts" by one
        reader and "3" by another.
        """
        retries = count_limit(self.max_format_retries, minimum=0)
        return None if retries is None else retries + 1

    @property
    def unlimited(self) -> bool:
        """Whether nothing here bounds the run but the user (DR-030)."""
        return (
            self.validation_iterations is None
            and self.time_budget is None
            and self.stage_attempts is None
            and count_limit(self.max_tool_rounds) is None
        )


DEFAULT_BOUNDS = WorkflowBounds()


def _bounds_from_tuning(tier: str, tuning: Tuning) -> WorkflowBounds:
    """A repo's configured tuning for *tier*, in `WorkflowBounds` shape (DR-021).

    Each staged tier has its own `Tuning` section (`tier` names it directly),
    so a repo can bound one tier without bounding another. A default
    `Tuning()` leaves every field unlimited (DR-030).
    """
    bounds = getattr(tuning, tier)
    return WorkflowBounds(
        max_validation_iterations=bounds.max_validation_iterations,
        time_budget_seconds=bounds.time_budget_seconds,
        max_format_retries=bounds.max_format_retries,
        max_tool_rounds=bounds.max_tool_rounds,
    )


class _Clock:
    """The wall-clock budget, isolated so tests can drive it deterministically.

    A budget of ``None`` — the default now (DR-030) — still times the run, and
    still reports how long it took; it simply never declares itself exhausted.
    """

    def __init__(
        self, budget_seconds: float | None, now: Callable[[], float] | None = None
    ) -> None:
        self._now = now or time.monotonic
        self._budget = duration_limit(budget_seconds)
        self._started = self._now()

    @property
    def elapsed(self) -> float:
        return self._now() - self._started

    @property
    def remaining(self) -> float | None:
        """Seconds left, or ``None`` when the run is untimed."""
        if self._budget is None:
            return None
        return max(0.0, self._budget - self.elapsed)

    @property
    def exhausted(self) -> bool:
        return self._budget is not None and self.elapsed >= self._budget


class StagedWorkflow:
    """Runs an ordered list of stages with bounded validation loopback."""

    def __init__(
        self,
        tier: str,
        stages: Sequence[str],
        *,
        bounds: WorkflowBounds | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._tier = tier
        self._stages = tuple(stages)
        # None means "derive from the request's configured tuning" (DR-021);
        # an explicit value here — as tests pass — always wins over that.
        self._explicit_bounds = bounds
        self._clock_source = clock
        if VALIDATE not in self._stages:
            raise ValueError(f"'{tier}' has no validation stage to gate on")

    def _bounds_for(self, request: WorkflowRequest) -> WorkflowBounds:
        return self._explicit_bounds or _bounds_from_tuning(self._tier, request.tuning)

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def stages(self) -> tuple[str, ...]:
        return self._stages

    # -- entry points ------------------------------------------------------

    def run(self, request: WorkflowRequest) -> WorkflowOutcome:
        stream = self.stream(request)
        try:
            while True:
                next(stream)
        except StopIteration as stop:
            outcome: WorkflowOutcome = stop.value
            return outcome

    def stream(
        self, request: WorkflowRequest
    ) -> Generator[str, None, WorkflowOutcome]:
        """Run every stage, yielding the final answer text at the end.

        Only the final answer is *yielded*. A stage's own prose is streamed as
        `stage_output_delta` events instead, because a plan and an execution
        summary are distinct artifacts an Adapter renders in their own right —
        concatenating four stages' output into one answer would be nonsense to
        read, and would make the answer unusable as session history (DR-004).
        """
        memory = RequestMemory(request.prompt)
        bounds = self._bounds_for(request)
        clock = _Clock(bounds.time_budget, self._clock_source)
        loopbacks = 0
        index = 0
        feedback = ""
        emit = _Emitter(request.events, self._tier)

        emit.workflow_started(self._stages, bounds)
        try:
            while index < len(self._stages):
                self._check_time(clock, bounds, self._stages[index], loopbacks)
                self._check_cancelled(request.cancel, self._stages[index], memory)
                stage = STAGES[self._stages[index]]

                emit.stage_started(stage, iteration=loopbacks + 1)
                result = self._run_stage(
                    stage, request, memory, feedback, emit, clock, bounds
                )
                feedback = ""

                if stage.name == VALIDATE:
                    verdict = self._judge(result.content, memory)
                    emit.verdict(verdict, memory)
                    if verdict.passed:
                        break
                    loopbacks += 1
                    cap = bounds.validation_iterations
                    if cap is not None and loopbacks >= cap:
                        raise IterationBudgetExhausted(
                            f"Validation rejected the work {loopbacks} time(s), "
                            "reaching the cap. The task is NOT complete. Last "
                            f"feedback: {verdict.feedback}",
                            stage=VALIDATE,
                            iterations=loopbacks,
                        )
                    memory.record_attempt(verdict.target, verdict.feedback)
                    feedback = verdict.feedback
                    index = self._index_of(verdict.target)
                    emit.loopback(verdict, loopbacks, cap)
                    continue

                index += 1

            answer = self._final_answer(memory)
            emit.workflow_finished("completed", clock.elapsed)
            yield answer
            return WorkflowOutcome(
                tier=self._tier,
                answer=answer,
                stop_reason="completed",
                usage=emit.usage,
                artifacts=memory.artifacts,
                files_changed=memory.files_changed,
                changes=_recorded_changes(request),
            )
        except RunCancelled as exc:
            # DR-030: stopping on purpose is not a failure, and everything the
            # run had already produced is kept — the artifacts each finished
            # stage wrote, and the journal `/undo` reverts from (DR-024). It is
            # still not a completed task, so `succeeded` stays false.
            emit.workflow_finished("cancelled", clock.elapsed, detail=str(exc))
            yield str(exc)
            return WorkflowOutcome(
                tier=self._tier,
                answer=str(exc),
                stop_reason="cancelled",
                usage=emit.usage,
                artifacts=memory.artifacts,
                files_changed=memory.files_changed,
                changes=_recorded_changes(request),
            )
        except (IterationBudgetExhausted, TimeBudgetExhausted) as exc:
            # DR-009: a bound being hit is its own outcome, never a success.
            emit.workflow_finished(_bound_reason(exc), clock.elapsed, detail=str(exc))
            yield str(exc)
            return WorkflowOutcome(
                tier=self._tier,
                answer=str(exc),
                stop_reason=_bound_reason(exc),
                usage=emit.usage,
                artifacts=memory.artifacts,
                files_changed=memory.files_changed,
                changes=_recorded_changes(request),
            )
        except StageExecutionError as exc:
            emit.workflow_finished("stage_failed", clock.elapsed, detail=str(exc))
            yield str(exc)
            return WorkflowOutcome(
                tier=self._tier,
                answer=str(exc),
                stop_reason="stage_failed",
                usage=emit.usage,
                artifacts=memory.artifacts,
                files_changed=memory.files_changed,
                changes=_recorded_changes(request),
            )

    # -- one stage ----------------------------------------------------------

    def _run_stage(
        self,
        stage: StageSpec,
        request: WorkflowRequest,
        memory: RequestMemory,
        feedback: str,
        emit: _Emitter,
        clock: _Clock,
        bounds: WorkflowBounds,
    ) -> AgentResult:
        """Run one stage, re-attempting format failures until told to stop.

        "Until told to stop" is the default (DR-030): `stage_attempts` is
        ``None`` unless a repo configured `max_format_retries`, and the loop
        then ends only when the stage produces a valid output or the user asks
        the run to stop.
        """
        complaint = ""
        attempt = 0
        for attempt in counted(bounds.stage_attempts):
            self._check_time(clock, bounds, stage.name, attempt)
            self._check_cancelled(request.cancel, stage.name, memory)
            agent = self._build_agent(stage, request, memory, bounds)
            prompt = self._stage_prompt(stage, request, memory, feedback, complaint)

            try:
                result = self._call_stage(agent, stage, prompt, request, emit)
            except ContextBudgetError as exc:
                # DR-009: a context-budget failure is a typed stage failure,
                # never a silently degraded artifact.
                raise StageExecutionError(
                    stage.name, str(exc), recoverable=False
                ) from exc

            emit.add_usage(result.usage)
            if result.stop_reason == "cancelled":
                # The agent stopped mid-round because the user asked it to.
                # Retrying would be answering a stop request with more work.
                raise RunCancelled(
                    _cancellation_message(stage.name, memory, request.cancel.reason),
                    stage=stage.name,
                    reason=request.cancel.reason,
                )
            if not result.complete:
                complaint = (
                    "Your previous attempt stopped before finishing "
                    f"({result.stop_reason}). Produce the complete output this time."
                )
                emit.stage_retry(stage, attempt, complaint)
                continue

            try:
                self._adopt(stage, result.content, memory)
            except (RequirementsFormatError, ArtifactTooLargeError) as exc:
                complaint = str(exc)
                emit.stage_retry(stage, attempt, complaint)
                continue

            emit.stage_finished(stage, result, memory)
            return result

        # Reachable only where a repo configured `max_format_retries`; with the
        # default the loop above ends by succeeding or by being stopped.
        raise StageExecutionError(
            stage.name,
            f"could not produce a valid output after {attempt} attempt(s). "
            f"Last problem: {complaint}",
        )

    def _call_stage(
        self,
        agent: StageAgent,
        stage: StageSpec,
        prompt: str,
        request: WorkflowRequest,
        emit: _Emitter,
    ) -> AgentResult:
        """Run one stage, forwarding its prose as it is produced.

        Streamed rather than blocking: on the hardware DR-015 targets a stage
        can run for minutes, and an interface with nothing to show through it
        is indistinguishable from one that has hung. DR-014 makes this free —
        a streamed round carries its own tool calls, so there is no second
        generation to pay for.
        """
        # Session history is projected into the first stage only. Later stages
        # work from canonical artifacts, not from conversation (DR-004).
        session_context = (
            list(request.session_context) if stage.name == self._stages[0] else None
        )
        stream = agent.stream(prompt, session_context=session_context)
        for chunk in stream:
            emit.stage_output(stage, chunk)
        result = stream.result
        assert result is not None, "an exhausted agent stream must produce a result"
        return result

    def _build_agent(
        self,
        stage: StageSpec,
        request: WorkflowRequest,
        memory: RequestMemory,
        bounds: WorkflowBounds,
    ) -> StageAgent:
        """A fresh agent, and therefore a fresh registry and memory, per attempt.

        DR-004 requires a loopback re-invocation start with a clean
        ``AgentMemory`` so a retry does not inherit the reasoning that failed;
        DR-008 requires the registry be this instance's own.
        """
        tuning = request.tuning
        agent = StageAgent(
            stage,
            workspace=request.workspace,
            model=request.model,
            budget=ContextBudget.from_tuning(
                tuning.context, context_limit=request.context_limit
            ),
            approval_gate=request.approval_gate,
            approval_policy=request.approval_policy,
            events=request.events,
            max_tool_rounds=bounds.max_tool_rounds,
            cancel=request.cancel,
            command_timeout=tuning.shell.command_timeout_seconds,
            max_edits_per_call=tuning.editing.max_edits_per_call,
            bounded_output=BoundedToolOutput(
                FileToolOutputStore(
                    request.workspace, max_artifacts=tuning.tool_output.max_artifacts
                ),
                max_chars=tuning.tool_output.max_chars,
            ),
            system_prompt_template=resolve_prompt(
                stage.name, stage.system_prompt, request.prompt_overrides
            ),
        )
        if stage.can_write:
            # Layered *over* whatever the stage's own guard installed, so a
            # stage-specific restriction still applies and only its accepted
            # writes are reported as changed files.
            _track_changes(agent, memory, request.journal)
        return agent

    def _adopt(self, stage: StageSpec, content: str, memory: RequestMemory) -> None:
        """Make a stage's output canonical, or refuse it.

        Parsing happens *before* the artifact is stored, so a malformed ledger
        blocks that output from becoming canonical state rather than corrupting
        it (DR-005).
        """
        requirements: list[Requirement] | None = None
        if stage.owns_ledger:
            requirements = parse_requirements(
                content, allowed_statuses=stage.allowed_statuses
            )
            if memory.requirements:
                require_same_ids(list(memory.requirements), requirements)

        if stage.artifact:
            # The ledger is stripped from the stored artifact: RequestMemory
            # owns it in parsed form, and a second textual copy could drift.
            memory.put(stage.artifact, strip_requirements_section(content).strip())
        if requirements is not None:
            memory.requirements = tuple(requirements)

    # -- prompting ------------------------------------------------------------

    def _stage_prompt(
        self,
        stage: StageSpec,
        request: WorkflowRequest,
        memory: RequestMemory,
        feedback: str,
        complaint: str,
    ) -> str:
        """Assemble one stage's input from canonical state only."""
        parts = [f"# Request\n\n{request.prompt}"]

        for name, heading in (
            ("plan", "Implementation plan"),
            ("execution_summary", "What the implementation stage reported"),
        ):
            if name == stage.artifact or not memory.has(name):
                continue
            if not self._precedes(name, stage.name):
                continue
            parts.append(f"# {heading}\n\n{memory.get(name)}")

        if memory.requirements and stage.owns_ledger:
            parts.append(
                "# Requirements ledger (carry these IDs forward exactly)\n\n"
                + format_requirements(list(memory.requirements))
            )

        digest = memory.retry_digest(stage.name)
        if digest:
            parts.append(f"# Earlier attempts\n\n{digest}")
        if feedback:
            parts.append(
                "# Why this was sent back\n\n"
                f"{feedback}\n\nAddress this specifically."
            )
        if complaint:
            parts.append(
                "# Your last response was rejected\n\n"
                f"{complaint}\n\nProduce the output again, correctly this time."
            )
        return "\n\n".join(parts)

    def _precedes(self, artifact: str, stage_name: str) -> bool:
        """Whether the stage producing *artifact* runs before *stage_name*."""
        producer = next(
            (name for name, spec in STAGES.items() if spec.artifact == artifact), ""
        )
        if producer not in self._stages or stage_name not in self._stages:
            return False
        return self._stages.index(producer) < self._stages.index(stage_name)

    # -- verdict and bounds -----------------------------------------------------

    def _judge(self, report: str, memory: RequestMemory) -> Verdict:
        return derive_verdict(
            report,
            list(memory.requirements),
            available_targets=tuple(
                name for name in (PLAN, EXECUTE) if name in self._stages
            ),
        )

    def _check_time(
        self, clock: _Clock, bounds: WorkflowBounds, stage: str, iteration: int
    ) -> None:
        if clock.exhausted:
            raise TimeBudgetExhausted(
                f"The time budget ({describe_limit(bounds.time_budget, 'seconds')}) "
                f"ran out during the {stage} stage. The task is not complete.",
                stage=stage,
                iterations=iteration,
            )

    @staticmethod
    def _check_cancelled(
        cancel: CancelToken, stage: str, memory: RequestMemory
    ) -> None:
        """Unwind if the user has asked the run to stop (DR-030).

        Checked between stages and between stage attempts — the two points
        where no inference is in flight, so stopping costs nothing and every
        artifact already adopted is safe in `memory`.
        """
        if cancel.requested:
            raise RunCancelled(
                _cancellation_message(stage, memory, cancel.reason),
                stage=stage,
                reason=cancel.reason,
            )

    def _index_of(self, stage: str) -> int:
        return self._stages.index(stage) if stage in self._stages else 0

    def _final_answer(self, memory: RequestMemory) -> str:
        """What the user reads: prose describing what was done.

        Deliberately prose only. The changed-file list is already a structured
        field on the outcome, and an Adapter renders it from there — putting it
        in the answer too renders it twice, and worse, promotes a file listing
        into session history (DR-004), where it would be replayed as
        conversational context on every later turn.
        """
        parts = [memory.get("execution_summary") or memory.get("plan")]
        if memory.requirements:
            parts.append(summarise(list(memory.requirements)))
        return "\n\n".join(part for part in parts if part).strip()


def _track_changes(
    agent: StageAgent, memory: RequestMemory, journal: ChangeJournal | None
) -> None:
    """Record what a stage changed: the path for the run, the content for undo.

    Wraps whatever handler the agent already holds rather than replacing it,
    so a stage-specific restriction underneath stays in force and only its
    *accepted* writes are recorded. A refused write returns an error string, which is how a
    refusal is told apart from a change.
    """
    track_changes(agent, journal, on_change=memory.note_file_changed)


def _cancellation_message(stage: str, memory: RequestMemory, reason: str) -> str:
    """What the user reads when they stop a run themselves (DR-030).

    It names what survived, because that is the question a partial result
    raises: a `craft` run stopped during Execute still has its plan, and may
    have changed files that `/undo` can revert.
    """
    kept = ", ".join(sorted(memory.artifacts)) or "nothing yet"
    files = memory.files_changed
    why = f" ({reason})" if reason else ""
    parts = [
        f"Stopped at your request during the {stage} stage{why}. "
        "The task is NOT complete.",
        f"Kept so far: {kept}.",
    ]
    if files:
        parts.append(
            f"{len(files)} file(s) were already changed: {', '.join(files)}. "
            "Review them with /diff, or revert them with /undo."
        )
    return " ".join(parts)


def _recorded_changes(request: WorkflowRequest) -> tuple:
    """The journal's changes, or nothing when the caller supplied no journal."""
    return request.journal.changes if request.journal is not None else ()


def _bound_reason(exc: Exception) -> str:
    return (
        "time_budget_exhausted"
        if isinstance(exc, TimeBudgetExhausted)
        else "attempts_exhausted"
    )


class _Emitter:
    """Turns runner progress into events, and tallies usage along the way."""

    def __init__(self, events: EventSink, tier: str) -> None:
        self._events = events
        self._tier = tier
        from ..inference.types import Usage

        self.usage = Usage()

    def add_usage(self, usage) -> None:
        from ..inference.types import Usage

        self.usage = Usage(
            prompt_tokens=self.usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self.usage.completion_tokens + usage.completion_tokens,
        )

    def workflow_started(self, stages: Sequence[str], bounds: WorkflowBounds) -> None:
        # The normalised values, not the raw ``-1``: an Adapter rendering this
        # should not have to know the sentinel. `null` reads as "no limit" in
        # JSON, and `describe_limit` says it in words for a terminal.
        self._emit(
            "workflow_started",
            " → ".join(stages),
            tier=self._tier,
            stages=list(stages),
            max_validation_iterations=bounds.validation_iterations,
            time_budget_seconds=bounds.time_budget,
            unlimited=bounds.unlimited,
        )

    def stage_started(self, stage: StageSpec, *, iteration: int) -> None:
        self._emit(
            "stage_started",
            stage.label,
            tier=self._tier,
            stage=stage.name,
            title=stage.title,
            iteration=iteration,
            tools=list(stage.tool_names),
        )

    def stage_output(self, stage: StageSpec, chunk: str) -> None:
        self._emit("stage_output_delta", chunk, tier=self._tier, stage=stage.name)

    def stage_retry(self, stage: StageSpec, attempt: int, reason: str) -> None:
        self._emit(
            "stage_retry",
            reason,
            tier=self._tier,
            stage=stage.name,
            attempt=attempt,
            reason=reason,
        )

    def stage_finished(self, stage: StageSpec, result, memory: RequestMemory) -> None:
        self._emit(
            "stage_finished",
            stage.title,
            tier=self._tier,
            stage=stage.name,
            artifact=stage.artifact,
            content=memory.get(stage.artifact) if stage.artifact else "",
            requirements=[
                {
                    "id": item.id,
                    "status": item.status,
                    "description": item.description,
                    "evidence": item.evidence,
                }
                for item in memory.requirements
            ],
            tokens=result.usage.total_tokens,
        )

    def verdict(self, verdict: Verdict, memory: RequestMemory) -> None:
        self._emit(
            "validation_verdict",
            verdict.describe(),
            tier=self._tier,
            passed=verdict.passed,
            overridden=verdict.overridden,
            target=verdict.target,
            unmet=list(verdict.unmet),
            feedback=verdict.feedback,
            requirements=summarise(list(memory.requirements)),
        )

    def loopback(self, verdict: Verdict, used: int, cap: int | None) -> None:
        self._emit(
            "loopback",
            f"back to {verdict.target} ({used}/{describe_limit(cap)})",
            tier=self._tier,
            target=verdict.target,
            iteration=used,
            cap=cap,
            feedback=verdict.feedback,
        )

    def workflow_finished(
        self, reason: str, elapsed: float, *, detail: str = ""
    ) -> None:
        self._emit(
            "workflow_finished",
            detail or reason,
            tier=self._tier,
            stop_reason=reason,
            succeeded=reason == "completed",
            elapsed_seconds=round(elapsed, 1),
        )

    def _emit(self, event_type: str, message: str = "", **data) -> None:
        self._events.emit(Event(type=event_type, message=message, data=data))
