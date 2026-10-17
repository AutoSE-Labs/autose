"""Typed error hierarchy shared across ``src/logic``.

DR-009 requires that every failure a stage or runner can hit be raised as a
*distinguishable, typed* error rather than a generic exception, so callers and
Adapters can present "ran out of time", "ran out of attempts", "the request
does not fit the context window" and "the backend is unreachable" as different
outcomes. Everything raised out of ``logic`` therefore descends from
:class:`AutoSEError`.
"""

from __future__ import annotations

__all__ = [
    "AutoSEError",
    "ConfigurationError",
    "WorkspaceEscapeError",
    "InferenceError",
    "InferenceTimeoutError",
    "ProviderContextLengthError",
    "ContextBudgetError",
    "StageExecutionError",
    "WorkflowBoundExhausted",
    "IterationBudgetExhausted",
    "TimeBudgetExhausted",
    "RunCancelled",
]


class AutoSEError(Exception):
    """Root of every error AutoSE's logic layer raises on purpose."""


class ConfigurationError(AutoSEError):
    """Runtime configuration is missing, malformed, or internally inconsistent."""


class WorkspaceEscapeError(AutoSEError):
    """A path resolved outside the workspace root and was refused."""


class InferenceError(AutoSEError):
    """A call to the configured OpenAI-compatible backend failed."""


class InferenceTimeoutError(InferenceError):
    """The inference backend did not respond in time, or was unreachable."""


class ProviderContextLengthError(InferenceError):
    """The *provider* rejected the request for exceeding its context window.

    Distinct from :class:`ContextBudgetError`, which is AutoSE's own local
    budget refusing to send a request it predicts will not fit (DR-010).
    """


class ContextBudgetError(AutoSEError):
    """The request cannot be made to fit the locally-calibrated context budget.

    Raised by :class:`logic.common.context.ContextBudget` *before* any network
    call. Per DR-009 this is a fail-closed outcome: the caller must surface it
    as a typed stage failure, never degrade it into a truncated artifact.
    """


class StageExecutionError(AutoSEError):
    """One stage of a multi-stage workflow could not produce a valid output.

    Carries which stage failed, so a runner can route feedback and a UI can say
    where the work stopped. DR-009 requires this be a typed failure rather than
    a generic exception: "the plan's ledger was malformed" and "the backend is
    unreachable" call for different responses.
    """

    def __init__(self, stage: str, message: str, *, recoverable: bool = True) -> None:
        super().__init__(f"{stage} stage failed: {message}")
        self.stage = stage
        self.detail = message
        #: Whether re-running the stage could plausibly succeed. A malformed
        #: ledger is recoverable; a context budget that cannot fit the stage
        #: input is not.
        self.recoverable = recoverable


class WorkflowBoundExhausted(AutoSEError):
    """A runner-level bound stopped the workflow (DR-009).

    Deliberately *not* a subclass of :class:`StageExecutionError`: no stage
    failed. The workflow ran out of the budget it was given, which is a
    different thing to tell the user, and — per DR-005 and DR-009 — must never
    be presented as a completed task.
    """

    def __init__(self, message: str, *, stage: str = "", iterations: int = 0) -> None:
        super().__init__(message)
        self.stage = stage
        self.iterations = iterations


class IterationBudgetExhausted(WorkflowBoundExhausted):
    """Validation sent work back more times than the cap allows."""


class TimeBudgetExhausted(WorkflowBoundExhausted):
    """The request's wall-clock budget ran out mid-workflow."""


class RunCancelled(AutoSEError):
    """The person driving the run asked it to stop (DR-030).

    Its own type, and deliberately not a :class:`WorkflowBoundExhausted`: no
    budget was exceeded and nothing failed. Since DR-030 makes every bound
    unlimited by default, stopping by hand is the *ordinary* way a long run
    ends, so this carries what a partial result needs — which stage was
    interrupted, and why — rather than reading as a failure.
    """

    def __init__(self, message: str, *, stage: str = "", reason: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        #: What the Adapter said when it asked to stop, if it said anything.
        self.reason = reason
