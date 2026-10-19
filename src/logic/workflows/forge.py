"""The `forge` tier: Design → Plan → Execute → Validate (DR-003).

For architecture-level work, changes spanning many files, or a task that
requires the system to make significant design decisions of its own. It is
`craft` with one stage added in front. That stage reads and reasons but writes
nothing: its note is handed to Plan and Execute as the approach already
decided. (The Pro edition's Design stage also writes a decision record; DR-054.)

Because a Design stage exists, validation may send work all the way back to it
when the flaw is architectural rather than an implementation mistake. That is a
more expensive loopback, and this tier is chosen for work where one more
attempt is likelier to be worth its cost. It keeps a separate `tuning:` section
from `craft`, so a repo that wants to bound one need not bound the other.
"""

from __future__ import annotations

from ..tiers import FORGE
from .base import register_workflow
from .staged import StagedWorkflow, WorkflowBounds
from .stages import DESIGN, EXECUTE, PLAN, VALIDATE

__all__ = ["ForgeWorkflow", "FORGE_STAGES"]

FORGE_STAGES: tuple[str, ...] = (DESIGN, PLAN, EXECUTE, VALIDATE)


class ForgeWorkflow(StagedWorkflow):
    def __init__(self, bounds: WorkflowBounds | None = None, **kwargs) -> None:
        super().__init__(FORGE, FORGE_STAGES, bounds=bounds, **kwargs)


register_workflow(FORGE, ForgeWorkflow)
