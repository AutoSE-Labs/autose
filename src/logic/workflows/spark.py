"""The ``spark`` tier: one direct pass (DR-003).

DR-003 defines this tier by what it deliberately lacks: no planning stage, no
requirements ledger, no multi-stage flow. It answers a question, or makes at
most one small self-contained change — a line, a value, a comment.

Its tool registry follows from that. It reads and searches freely, and it can
run an approved command to check something, because those are how it answers
questions honestly rather than guessing. It has ``edit_file`` for the trivial
change the tier permits, and it does **not** have ``write_file``: creating a
file or rewriting one wholesale is past what "near-no-write" means, and a
request needing it belongs in ``craft``.
"""

from __future__ import annotations

from collections.abc import Generator
from functools import partial

from ..common.agent import BaseAgent
from ..common.changes import track_changes
from ..common.context import ContextBudget
from ..common.tool_output import BoundedToolOutput, FileToolOutputStore
from ..common.tools import READ_ONLY_TOOL_NAMES, catalog_subset
from ..common.tools.editing import edit_file as _edit
from ..common.tools.shell import run_command as _run_command
from ..inference.types import Usage
from ..prompt_overrides import format_prompt, resolve_prompt
from ..tiers import SPARK
from .base import WorkflowOutcome, WorkflowRequest, register_workflow

__all__ = ["SparkAgent", "SparkWorkflow", "SPARK_TOOL_NAMES"]

#: Read, search, check — plus the one trivial edit the tier allows.
SPARK_TOOL_NAMES: tuple[str, ...] = READ_ONLY_TOOL_NAMES + (
    "run_command",
    "edit_file",
)

_SYSTEM_PROMPT = """\
You are AutoSE running in `spark` mode: the fast, lightweight tier.

You are working inside this workspace:
  {workspace}

Use it for the smallest kind of request — answering a question about the code,
or making one small, self-contained change (a single line, one value or config
setting, a comment).

How to work:
- Look before you answer. Use `search_files` and `find_files` to locate things,
  and `read_file` to read them. Never describe code you have not read.
- Prefer the file and git tools over `run_command`. Running a command
  interrupts the user for approval, so reserve it for what nothing else can
  tell you — a test run, an installed version. Repository state has its own
  tools: `git_status`, `git_diff`, `git_log`.
- If you change something, use `edit_file` with the exact text as it appears on
  disk right now. Make the change in one call.
- Answer in plain prose. Be specific and brief: name files as `path:line` so
  they can be opened directly. Do not pad the answer with a summary of what you
  are about to say, or a restatement of the question.
- If you do not know, say so and say what you would need to find out.

If the request turns out to need real planning — several files, a new module,
a design decision, anything you cannot finish in one pass — do not attempt it.
Say plainly that it needs the `craft` tier and why, in one or two
sentences, and stop.
"""


class SparkAgent(BaseAgent):
    """The single agent behind the ``spark`` tier."""

    name = SPARK

    def __init__(self, *, system_prompt_template: str | None = None, **kwargs) -> None:
        self._system_prompt_template = system_prompt_template or _SYSTEM_PROMPT
        super().__init__(**kwargs)

    def system_prompt(self) -> str:
        return format_prompt(self._system_prompt_template, workspace=self.workspace.root)


class SparkWorkflow:
    """One direct pass. No stages, no ledger, no loopback."""

    @property
    def tier(self) -> str:
        return SPARK

    def run(self, request: WorkflowRequest) -> WorkflowOutcome:
        agent = self._build(request)
        result = agent.run(
            request.prompt, session_context=list(request.session_context)
        )
        return self._outcome(result, request)

    def stream(
        self, request: WorkflowRequest
    ) -> Generator[str, None, WorkflowOutcome]:
        agent = self._build(request)
        stream = agent.stream(
            request.prompt, session_context=list(request.session_context)
        )
        yield from stream
        return self._outcome(stream.result, request)

    # -- internals ------------------------------------------------------

    @staticmethod
    def _build(request: WorkflowRequest) -> SparkAgent:
        tuning = request.tuning
        agent = SparkAgent(
            model=request.model,
            tools=catalog_subset(*SPARK_TOOL_NAMES),
            workspace=request.workspace,
            budget=ContextBudget.from_tuning(
                tuning.context, context_limit=request.context_limit
            ),
            approval_gate=request.approval_gate,
            approval_policy=request.approval_policy,
            events=request.events,
            max_tool_rounds=tuning.spark.max_tool_rounds,
            cancel=request.cancel,
            bounded_output=BoundedToolOutput(
                FileToolOutputStore(
                    request.workspace, max_artifacts=tuning.tool_output.max_artifacts
                ),
                max_chars=tuning.tool_output.max_chars,
            ),
            tool_overrides={
                "run_command": partial(
                    _run_command, timeout=tuning.shell.command_timeout_seconds
                ),
                "edit_file": partial(
                    _edit, max_edits_per_call=tuning.editing.max_edits_per_call
                ),
            },
            system_prompt_template=resolve_prompt(
                "spark_system", _SYSTEM_PROMPT, request.prompt_overrides
            ),
        )
        # `spark` has `edit_file`, so it can change the workspace — and until
        # DR-024 it was the one tier that never said which file it had
        # touched, because only the staged runner tracked writes.
        track_changes(agent, request.journal)
        return agent

    @staticmethod
    def _outcome(result, request: WorkflowRequest) -> WorkflowOutcome:
        changes = request.journal.changes if request.journal is not None else ()
        paths = tuple(change.path for change in changes)
        if result is None:
            return WorkflowOutcome(
                tier=SPARK,
                answer="",
                stop_reason="no_result",
                usage=Usage(),
                files_changed=paths,
                changes=changes,
            )
        return WorkflowOutcome(
            tier=SPARK,
            answer=result.content,
            # DR-009: an exhausted round budget is not a success, and must not
            # be rendered as one.
            stop_reason="completed" if result.complete else result.stop_reason,
            usage=result.usage,
            files_changed=paths,
            changes=changes,
        )


register_workflow(SPARK, SparkWorkflow)
