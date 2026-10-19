"""A person reviews the plan before anything is changed (DR-045).

The Strategy seam for it, in the same shape as ``ApprovalGate`` (DR-012): the
staged runner asks a ``PlanReviewGate`` once the Plan stage has produced a
plan, and an adapter decides what the question looks like. With no gate
attached — the terminal by default, the JSON adapter, an evaluation harness —
nothing changes: the run goes straight from Plan to Execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = ["PlanDecision", "PlanReview", "PlanReviewGate", "PlanAction"]

PlanAction = Literal["approve", "revise", "edit", "stop"]


@dataclass(frozen=True)
class PlanReview:
    """What a reviewer is shown: the plan, with its requirements ledger."""

    #: The plan as the next stage would read it, ledger included, so an edit
    #: can change the requirements as well as the steps.
    plan: str
    requirements: tuple[dict[str, str], ...]
    #: 1 for the first plan, 2 after one revision, and so on.
    pass_number: int = 1
    #: Why a previous edit was not accepted, when the reviewer is being asked
    #: again after one; empty otherwise.
    problem: str = ""


@dataclass(frozen=True)
class PlanDecision:
    """The reviewer's answer."""

    action: PlanAction
    #: What to change, for ``revise``; the reason, for ``stop``.
    feedback: str = ""
    #: The replacement plan, for ``edit``. It is checked exactly as the
    #: model's own plan is — it must still carry a valid requirements ledger.
    plan: str = ""

    @classmethod
    def approve(cls) -> PlanDecision:
        return cls("approve")

    @classmethod
    def revise(cls, feedback: str) -> PlanDecision:
        return cls("revise", feedback=feedback)

    @classmethod
    def edit(cls, plan: str) -> PlanDecision:
        return cls("edit", plan=plan)

    @classmethod
    def stop(cls, reason: str = "") -> PlanDecision:
        return cls("stop", feedback=reason)


@runtime_checkable
class PlanReviewGate(Protocol):
    """Decides whether a plan goes ahead, and how."""

    def review(self, review: PlanReview) -> PlanDecision:
        """Block until the plan is approved, sent back, replaced or stopped."""
