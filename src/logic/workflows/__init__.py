"""Per-tier workflow shapes (DR-003).

Importing this package registers every implemented tier. Tiers whose workflow
is not written yet are simply absent from the registry, so
:func:`available_tiers` reports what can actually run rather than what is
planned.
"""

from __future__ import annotations

from .base import (
    UnavailableTierError,
    Workflow,
    WorkflowFactory,
    WorkflowOutcome,
    WorkflowRequest,
    available_tiers,
    create_workflow,
    register_workflow,
    supports_tier,
)
from .craft import CRAFT_STAGES, CraftWorkflow
from .spark import SparkAgent, SparkWorkflow
from .staged import DEFAULT_BOUNDS, StagedWorkflow, WorkflowBounds
from .stages import STAGES, StageAgent, StageSpec
from .verdict import Verdict, derive_verdict

__all__ = [
    "CRAFT_STAGES",
    "CraftWorkflow",
    "DEFAULT_BOUNDS",
    "STAGES",
    "SparkAgent",
    "StageAgent",
    "StageSpec",
    "StagedWorkflow",
    "Verdict",
    "WorkflowBounds",
    "derive_verdict",
    "SparkWorkflow",
    "UnavailableTierError",
    "Workflow",
    "WorkflowFactory",
    "WorkflowOutcome",
    "WorkflowRequest",
    "available_tiers",
    "create_workflow",
    "register_workflow",
    "supports_tier",
]
