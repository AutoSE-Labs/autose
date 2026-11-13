"""What this run changed, as a diff, for the validate stage (DR-071).

Validate starts with a clean memory (DR-004): it gets the request, the plan
and Execute's own report of what it did, but not the change itself. On
qwen3:8b it then spent its first turns listing the workspace and re-reading
the files Plan and Execute had already read — about 35 of its 72 seconds in
the fastest run measured — and in another run approved a test that had never
been written, on Execute's word that it had.

The run's change journal (DR-024) already holds each file as it was before the
run. Diffed against the file on disk now, that is exactly what changed, in a
form Execute cannot misreport.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from ..common.changes import DEFAULT_MAX_SNAPSHOT_BYTES, ChangeJournal, FileChange
from ..common.workspace import Workspace
from ..errors import WorkspaceEscapeError

__all__ = ["RunChanges", "commands_section", "run_changes"]

#: At most this many characters of diff in all. A file whose diff would go
#: over it is named but left out, and Validate reads that file itself.
MAX_CHARS = 12_000

_HEADING = "# What changed in this run"
_NOTE = (
    "Computed by AutoSE from each file as it was before this run and as it is "
    "on disk now. This is the change itself, not the implementation stage's "
    "account of it. Files changed only by a shell command are not shown."
)

_COMMANDS_HEADING = "# Commands run after the last edit"
_COMMANDS_NOTE = (
    "Run by the implementation stage and recorded by AutoSE as each finished, "
    "with no edit made by the write tools since, so the output is from the "
    "code as it is now. The implementation stage chose these commands and may "
    "have written the tests they run: judge whether they check what the "
    "request asks."
)


@dataclass(frozen=True)
class RunChanges:
    """The diff section for Validate's input."""

    section: str = ""


def run_changes(journal: ChangeJournal | None, workspace: Workspace) -> RunChanges:
    """What the run's write tools changed, measured against the disk now."""
    if journal is None:
        return RunChanges()
    current = [
        change
        for change in (_as_on_disk(change, workspace) for change in journal.changes)
        if change.before != change.after or not change.restorable
    ]
    if not current:
        return RunChanges()


    blocks: list[str] = []
    left_out: list[str] = []
    used = 0
    for change in current:
        block = change.diff()
        if used + len(block) > MAX_CHARS:
            left_out.append(change.path)
            continue
        blocks.append(block)
        used += len(block)
    parts = [_HEADING, _NOTE]
    if blocks:
        parts.append("```diff\n" + "\n".join(blocks) + "\n```")
    if left_out:
        parts.append(
            "Also changed, but too large to show here, so read them yourself: "
            + ", ".join(left_out)
            + "."
        )
    return RunChanges(
        section="\n\n".join(parts)
    )


def commands_section(commands: Sequence[tuple[str, str]]) -> str:
    """The commands run after the last edit, for Validate's input (DR-072).

    Validate is told to mark a requirement `Verified` only for a check that
    ran. When the implementation stage had already run the tests and they
    passed, Validate could not see that, so on qwen3:8b it often answered
    `Unverified` without running anything, and a whole second Validate pass
    followed. AutoSE recorded the output itself, so it is evidence, not the
    implementation stage's account; it is passed on only while no write since
    has made it stale.
    """
    if not commands:
        return ""
    blocks = [f"$ {command}\n{output.rstrip()}" for command, output in commands]
    return "\n\n".join(
        [
            _COMMANDS_HEADING,
            _COMMANDS_NOTE,
            "```\n" + "\n\n".join(blocks) + "\n```",
        ]
    )


def _as_on_disk(change: FileChange, workspace: Workspace) -> FileChange:
    """*change* with its after side re-read from disk.

    The journal's after side is what the last write tool left. A command run
    since may have changed the file again, and Validate judges the file as it
    is.
    """
    if not change.restorable:
        return change
    try:
        target = workspace.resolve(change.path)
        if not target.is_file():
            return replace(change, after=None)
        if target.stat().st_size > DEFAULT_MAX_SNAPSHOT_BYTES:
            return change
        return replace(change, after=target.read_bytes())
    except (WorkspaceEscapeError, OSError, ValueError):
        return change
