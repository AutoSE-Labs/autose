"""The workflow Strategy seam — one interchangeable shape per tier (DR-003).

DR-003 gives each tier a materially different *workflow shape*, not just a
different budget: ``spark`` is one direct pass, ``craft`` is Plan → Execute →
Validate, looping back when validation fails. Expressing
that as a branch inside one runner would mean every tier's change runs through
code every other tier also runs through — exactly what DR-002 rejects.

So a tier's shape is a registered ``Workflow`` implementation. Adding one is a
new module plus a registration, with no edit to the session that selects it.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..common.approval import ApprovalGate, ApprovalPolicy
from ..common.cancel import CancelToken
from ..common.changes import ChangeJournal, FileChange
from ..common.events import EventSink
from ..common.workspace import Workspace
from ..errors import AutoSEError
from ..inference.base import ChatModel
from ..inference.types import Usage
from ..tuning import Tuning
from .review import PlanReviewGate

__all__ = [
    "WorkflowRequest",
    "WorkflowOutcome",
    "Workflow",
    "WorkflowFactory",
    "UnavailableTierError",
    "register_workflow",
    "create_workflow",
    "available_tiers",
    "supports_tier",
]


class UnavailableTierError(AutoSEError):
    """No workflow is registered for the requested tier."""


@dataclass(frozen=True)
class WorkflowRequest:
    """Everything one workflow run needs, assembled by the session facade."""

    prompt: str
    workspace: Workspace
    model: ChatModel
    events: EventSink
    approval_gate: ApprovalGate
    #: Which tools this run may not execute unreviewed (DR-012/DR-024). The
    #: default gates shell and network; a workspace may also gate edits.
    approval_policy: ApprovalPolicy = field(default_factory=ApprovalPolicy.default)
    #: The stop signal for this run (DR-030). Since every bound is unlimited
    #: by default, this is what ends a long run: a workflow checks it between
    #: stages and hands it to each agent, so the person driving can stop the
    #: work and keep what it has already produced. A default token is never
    #: requested, which is the right behaviour for a caller with no way to ask.
    cancel: CancelToken = field(default_factory=CancelToken)
    #: Where accepted file writes are recorded so the turn can be reviewed as
    #: a diff and reverted (DR-024). ``None`` disables journalling, which only
    #: a caller that has no use for undo should choose.
    journal: ChangeJournal | None = None
    #: Bounded session history, projected in by the caller. DR-004 keeps this
    #: an ephemeral projection: a workflow reads it, never writes it back.
    session_context: tuple[dict, ...] = ()
    context_limit: int | None = None
    #: Every tunable knob, as loaded from the repo's `.autose/config.yaml`
    #: (DR-021). A default `Tuning()` reproduces AutoSE's previously-hardcoded
    #: behaviour exactly, so an unconfigured repo is unaffected.
    tuning: Tuning = field(default_factory=Tuning)
    #: Named prompt replacements from the same file (DR-021); a workflow reads
    #: this via `logic.prompt_overrides.resolve_prompt` rather than assuming
    #: any name is present.
    prompt_overrides: Mapping[str, str] = field(default_factory=dict)
    #: Asked to approve, revise or replace the plan before Execute (DR-045).
    #: ``None`` — the default — runs straight from Plan to Execute.
    plan_review: PlanReviewGate | None = None


@dataclass(frozen=True)
class WorkflowOutcome:
    """What a workflow produced, in a form any Adapter can render."""

    tier: str
    answer: str
    #: ``"completed"`` on success; anything else names how it stopped —
    #: ``"cancelled"`` when the user stopped it (DR-030), ``"attempts_exhausted"``
    #: / ``"time_budget_exhausted"`` when a configured bound ran out, and so on.
    #: DR-009 forbids presenting any of those as a success, so :attr:`succeeded`
    #: is derived from this rather than assumed.
    stop_reason: str = "completed"
    usage: Usage = field(default_factory=Usage)
    #: Canonical artifacts the run produced (a design doc, a plan, a ledger).
    artifacts: Mapping[str, str] = field(default_factory=dict)
    files_changed: tuple[str, ...] = ()
    #: The before/after content of every file this run changed (DR-024).
    #: `files_changed` remains the bare path list an Adapter can render
    #: without reasoning about content.
    changes: tuple[FileChange, ...] = ()
    #: The requirements ledger as it stood when the run ended (DR-005), as
    #: plain data — what a reviewer reads to see why the run counts as done,
    #: or what was left unverified (DR-038). Empty for tiers without one.
    requirements: tuple[dict[str, str], ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "completed"


@runtime_checkable
class Workflow(Protocol):
    """One tier's workflow shape."""

    @property
    def tier(self) -> str:
        """The tier this workflow implements."""

    def run(self, request: WorkflowRequest) -> WorkflowOutcome:
        """Execute the workflow to completion."""

    def stream(
        self, request: WorkflowRequest
    ) -> Generator[str, None, WorkflowOutcome]:
        """Execute it, yielding user-facing text as it is produced.

        Returns the :class:`WorkflowOutcome` as the generator's return value,
        so a caller using ``yield from`` gets both without a second run. The
        `Generator[...]` annotation — rather than `Iterator[str]` — is what
        states that second half in the type rather than only in this
        docstring, and is what lets a checker verify the callers.
        """


WorkflowFactory = Callable[[], Workflow]

_WORKFLOWS: dict[str, WorkflowFactory] = {}


def register_workflow(
    tier: str, factory: WorkflowFactory, *, replace: bool = False
) -> None:
    """Register the workflow implementing *tier*."""
    from ..tiers import is_tier

    if not is_tier(tier):
        raise ValueError(f"'{tier}' is not a known tier; add it to logic.tiers first")
    if tier in _WORKFLOWS and not replace:
        raise ValueError(f"a workflow is already registered for tier '{tier}'")
    _WORKFLOWS[tier] = factory


def create_workflow(tier: str) -> Workflow:
    """Build the workflow for *tier*, or explain that it is not wired up yet."""
    factory = _WORKFLOWS.get(tier)
    if factory is None:
        ready = ", ".join(sorted(_WORKFLOWS)) or "none"
        raise UnavailableTierError(
            f"the '{tier}' tier is not implemented yet. Implemented tiers: {ready}."
        )
    return factory()


def available_tiers() -> tuple[str, ...]:
    """Tiers that actually have a workflow behind them, in rank order."""
    from ..tiers import TIER_RANK

    return tuple(sorted(_WORKFLOWS, key=lambda tier: TIER_RANK[tier]))


def supports_tier(tier: str) -> bool:
    return tier in _WORKFLOWS
