"""The `craft` tier: Plan → Execute → Validate (DR-003).

The tier for anything that needs a plan — a feature, a bug fix, a change
across many files. It is a stage list handed to the shared staged runner.

Validation may send work back to Plan or Execute, bounded by DR-009's caps.
There is no Design stage to return to; a validator asking for one is routed
to Execute.
"""

from __future__ import annotations

from ..tiers import CRAFT
from .base import register_workflow
from .staged import StagedWorkflow, WorkflowBounds
from .stages import EXECUTE, PLAN, VALIDATE

__all__ = ["CraftWorkflow", "CRAFT_STAGES"]

CRAFT_STAGES: tuple[str, ...] = (PLAN, EXECUTE, VALIDATE)


class CraftWorkflow(StagedWorkflow):
    def __init__(self, bounds: WorkflowBounds | None = None, **kwargs) -> None:
        super().__init__(CRAFT, CRAFT_STAGES, bounds=bounds, **kwargs)


register_workflow(CRAFT, CraftWorkflow)
