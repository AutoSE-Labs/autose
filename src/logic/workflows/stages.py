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
from ..common.tools.filesystem import write_file as _write
from ..common.tools.shell import COMMAND_TIMEOUT_SECONDS
from ..common.tools.shell import run_command as _run_command
from ..common.workspace import Workspace
from ..errors import WorkspaceEscapeError
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
    "validate_scratch_guard",
    "VALIDATE_SCRATCH_DIRECTORY",
]

PLAN = "plan"
EXECUTE = "execute"

#: Whether the execute stage must change at least one file before it may
#: finish (see BaseAgent.requires_changes). Tests whose scripted execute stage
#: legitimately writes nothing switch it off.
EXECUTE_REQUIRES_CHANGES = True

#: How many extra, independent validation passes must also approve before a
#: pass ends the run. One validator approving its own reading of the request
#: let through rules the request stated plainly ("unknown attributes are
#: ignored") while runs still had half an hour left. Tests switch it off.
CONFIRMING_VALIDATION_PASSES = 1

#: What the confirming validator is told. It does not see the first report:
#: the point is a second reading, not a second signature on the first.
CONFIRMING_VALIDATION_NOTICE = (
    "An earlier validation pass approved this work. You are an independent "
    "second check, and your job is to find what it missed. Go through the "
    "original request sentence by sentence. For every rule it states — what "
    "to accept, reject or ignore, exact output formats and separators, and "
    "each edge case it names — write a scratch script that feeds that exact "
    "case to the code and compare the result with the request's wording, not "
    "with the implementation's own tests. Fail the round for any mismatch."
)
VALIDATE = "validate"

#: Sent to a validator that stalled, with its tools removed. It may only
#: report what it actually confirmed.
_FORCED_VERDICT_NOTICE = (
    "Stop re-reading and write your verdict now, from what you have already "
    "confirmed. Mark a requirement Verified only if a check you ran in this "
    "stage (a test or a command) confirmed it; every other requirement is "
    "Unverified, and any Unverified requirement fails the round. Follow the "
    "required format exactly."
)


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


#: The one place the validate stage may write: throwaway check scripts. Removed
#: when the stage ends, so nothing in it reaches the result.
VALIDATE_SCRATCH_DIRECTORY = ".autose-scratch"

#: A rewrite that repeats at least this many characters of the script it
#: replaces, from its start, is reported as an append sent the expensive way.
_RESENT_SCRIPT_CHARS = 1500

_RESENT_SCRIPT_NOTICE = (
    "\n\nNote: this rewrite repeated the first {chars} characters already in "
    "{path}. Every character of a rewrite is generated again — one validation "
    "pass spent four minutes per round re-sending a growing script. To add "
    "checks, write them to a new script (e.g. check_more.py), or pass "
    "append=true with only the new lines."
)


def validate_scratch_guard(
    workspace: Workspace, *, max_edits_per_call: int = MAX_EDITS_PER_CALL
) -> Mapping[str, ToolHandler]:
    """Let the validate stage write check scripts, and nothing else.

    `run_command` refuses any command text containing an absolute path, which
    also refuses ordinary test data — a cookie's `Path=/`, a URL path, a JSON
    value. Validators then could not run a single independent check and fell
    back on the implementation's own tests. A script written to a file and run
    by name keeps that text out of the command. DR-008 still holds: the
    validator cannot touch the code it is judging.
    """
    root = workspace.resolve(VALIDATE_SCRATCH_DIRECTORY)

    def guarded_write(
        path: str, content: str, *, workspace: Workspace, **kwargs
    ) -> str:
        try:
            resolved = workspace.resolve(path)
        except (WorkspaceEscapeError, OSError, ValueError):
            resolved = None
        if resolved is None or root not in resolved.parents:
            return (
                "Error: the validate stage may only write check scripts inside "
                f"{VALIDATE_SCRATCH_DIRECTORY}/, e.g. "
                f"{VALIDATE_SCRATCH_DIRECTORY}/check_x.py. '{path}' is outside "
                "it. Report what is wrong instead of fixing it."
            )
        root.mkdir(parents=True, exist_ok=True)
        append = bool(kwargs.get("append"))
        try:
            before = "" if append else resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            before = ""
        result = _write(path, content, append, workspace=workspace)
        repeated = len(_common_prefix(before, content))
        if repeated >= _RESENT_SCRIPT_CHARS and not result.startswith("Error"):
            result += _RESENT_SCRIPT_NOTICE.format(chars=repeated, path=path)
        return result

    return {"write_file": guarded_write}


def _common_prefix(first: str, second: str) -> str:
    """The longest shared start of two strings."""
    limit = min(len(first), len(second))
    index = 0
    while index < limit and first[index] == second[index]:
        index += 1
    return first[:index]


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
        # `write_file` is confined to throwaway scripts (see
        # validate_scratch_guard), so it cannot change the work under review.
        tool_names=_read_only() + ("run_command", "write_file"),
        guard=validate_scratch_guard,
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
        # The execute stage exists to change files; finishing without a single
        # change is not a result (see BaseAgent.requires_changes).
        self.requires_changes = spec.name == EXECUTE and EXECUTE_REQUIRES_CHANGES
        if spec.name == VALIDATE:
            # A stalled validator answering with its tools removed used to
            # approve work it had not checked, ending runs an hour early.
            self.forced_answer_notice = _FORCED_VERDICT_NOTICE

    def system_prompt(self) -> str:
        return format_prompt(self._system_prompt_template, workspace=self.workspace.root)
