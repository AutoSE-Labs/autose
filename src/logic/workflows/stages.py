"""Stage definitions and their least-privilege tool registries (DR-008).

Each stage declares the tools it is entitled to *by name*. That is what makes
the permission auditable: the validation stage's registry has no ``write_file``
or ``edit_file`` in it, so the model is never told those exist. It is not
instructed to refrain — it structurally cannot.

A stage that needs more than a name list — write access confined to part of
the workspace, say — supplies a ``guard`` that wraps handlers on its own cloned
registry, because DR-008 is explicit that a prompt-level restriction is
guidance rather than enforcement.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from ..common.agent import BaseAgent
from ..common.tool import ToolHandler
from ..common.tools import READ_ONLY_TOOL_NAMES, catalog_subset
from ..common.tools.editing import MAX_EDITS_PER_CALL
from ..common.tools.editing import edit_file as _edit
from ..common.tools.shell import COMMAND_TIMEOUT_SECONDS
from ..common.tools.shell import run_command as _run_command
from ..common.workspace import Workspace
from ..prompt_overrides import format_prompt
from ..requirements import (
    EXECUTE_STATUSES,
    PLAN_STATUSES,
    VALIDATE_STATUSES,
)
from .prompts import EXECUTE_PROMPT, PLAN_PROMPT, VALIDATE_PROMPT

__all__ = [
    "PLAN",
    "EXECUTE",
    "VALIDATE",
    "StageSpec",
    "STAGES",
    "StageAgent",
]

PLAN = "plan"
EXECUTE = "execute"
VALIDATE = "validate"


@dataclass(frozen=True)
class StageSpec:
    """What one stage is: its prompt, its tools, and its ledger vocabulary."""

    name: str
    title: str
    system_prompt: str
    tool_names: tuple[str, ...]
    #: Ledger statuses this stage may emit. Empty when it owns no ledger.
    allowed_statuses: frozenset[str] = frozenset()
    #: Wraps handlers on this stage's cloned registry to narrow permissions.
    #: Takes the workspace plus the configured edit cap (DR-021), so a guard
    #: that wraps `edit_file` applies the same cap as every other stage.
    guard: Callable[..., Mapping[str, ToolHandler]] | None = None
    #: The artifact slot in ``RequestMemory`` this stage writes.
    artifact: str = ""
    label: str = ""

    @property
    def owns_ledger(self) -> bool:
        return bool(self.allowed_statuses)

    @property
    def can_write(self) -> bool:
        return bool({"write_file", "edit_file"} & set(self.tool_names))


def _read_only() -> tuple[str, ...]:
    """Every tool that only observes the workspace, from the one catalog list.

    Read from the catalog rather than restated here, so a read-only tool added
    there (DR-025's git handlers, for instance) reaches every stage that is
    entitled to observe, instead of reaching whichever stage lists it.
    """
    return READ_ONLY_TOOL_NAMES


STAGES: Mapping[str, StageSpec] = {
    PLAN: StageSpec(
        name=PLAN,
        title="Plan",
        label="Building the plan",
        system_prompt=PLAN_PROMPT,
        # Read-only, per DR-008. A planning stage that could edit would.
        tool_names=_read_only() + ("run_command",),
        allowed_statuses=PLAN_STATUSES,
        artifact="plan",
    ),
    EXECUTE: StageSpec(
        name=EXECUTE,
        title="Execute",
        label="Implementing",
        system_prompt=EXECUTE_PROMPT,
        tool_names=_read_only() + ("run_command", "write_file", "edit_file"),
        allowed_statuses=EXECUTE_STATUSES,
        artifact="execution_summary",
    ),
    VALIDATE: StageSpec(
        name=VALIDATE,
        title="Validate",
        label="Checking the result",
        # No write or edit tool in the schema at all (DR-008). This stage
        # reports gaps and routes them back; it never papers over its own
        # findings.
        system_prompt=VALIDATE_PROMPT,
        tool_names=_read_only() + ("run_command",),
        allowed_statuses=VALIDATE_STATUSES,
        artifact="validation_report",
    ),
}


class StageAgent(BaseAgent):
    """One stage's agent: a ``BaseAgent`` configured entirely by its spec."""

    def __init__(
        self,
        spec: StageSpec,
        *,
        workspace: Workspace,
        command_timeout: float = COMMAND_TIMEOUT_SECONDS,
        max_edits_per_call: int = MAX_EDITS_PER_CALL,
        system_prompt_template: str | None = None,
        **kwargs,
    ) -> None:
        self.spec = spec
        # DR-021: a repo's configured timeout/edit cap applies to every stage
        # that has these tools, and to a guarded edit_file (below) the same
        # way, not just the unrestricted registry.
        self._system_prompt_template = system_prompt_template or spec.system_prompt
        overrides = dict(kwargs.pop("tool_overrides", None) or {})
        overrides.setdefault(
            "run_command", partial(_run_command, timeout=command_timeout)
        )
        overrides.setdefault(
            "edit_file", partial(_edit, max_edits_per_call=max_edits_per_call)
        )
        if spec.guard is not None:
            overrides.update(
                spec.guard(workspace, max_edits_per_call=max_edits_per_call)
            )
        super().__init__(
            tools=catalog_subset(*spec.tool_names),
            workspace=workspace,
            tool_overrides=overrides,
            name=spec.name,
            **kwargs,
        )

    def system_prompt(self) -> str:
        return format_prompt(self._system_prompt_template, workspace=self.workspace.root)
