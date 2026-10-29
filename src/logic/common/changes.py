"""The workspace change journal: what a run wrote, and how to put it back (DR-024).

DR-012 gates the tools that reach *outside* the workspace. The tools that
rewrite the workspace itself were the gap: `write_file` and `edit_file` landed
their changes with no record of what the file said a moment earlier, so
"AutoSE changed three files" was the most a user could be told, and undoing a
turn meant reaching for git — which only helps if the work was committed
first.

A :class:`ChangeJournal` closes that. It wraps a mutating handler on an
agent's own cloned registry (DR-008's instrumentation seam), snapshots the
file either side of the call, and keeps the pair. From those pairs it can
render a unified diff of everything a turn did, and restore the workspace to
what it was before. The wrappers are `write_file` and `edit_file` only. An
approved ``run_command`` can change other files; those are not journalled,
and the summary, ``/diff``, and the dashboard say so (DR-024).

Three properties are load-bearing:

* **Per file, first-before and last-after.** Five edits to one file coalesce
  into one change whose *before* is what the file said when the turn started.
  Reverting therefore restores the original, not the fourth intermediate
  state, and the diff shows the net effect rather than a replay.
* **Revert verifies before it writes.** A file whose current content is no
  longer what the journal last wrote has been touched by someone else — the
  user, a formatter, a build. Restoring over that would destroy work AutoSE
  never made, so it is skipped and reported instead.
* **Bounded, and honest when it hits a bound.** A file too large to snapshot
  is recorded as changed but explicitly *not* restorable, rather than being
  silently held in memory or silently forgotten.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import WorkspaceEscapeError
from .tool import ToolHandler
from .workspace import Workspace

__all__ = [
    "FileChange",
    "ChangeJournal",
    "RevertReport",
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "DEFAULT_MAX_TRACKED_FILES",
    "track_changes",
    "CHANGED_SINCE",
]

#: Largest file either side of a change that is held in memory for undo. A
#: generated bundle or a checked-in binary blows past this; source does not.
DEFAULT_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024

#: How many distinct files one journal will track. A run touching more than
#: this has gone wrong in a way undo cannot help with.
DEFAULT_MAX_TRACKED_FILES = 500

#: Prefix every tool handler uses to report a refusal or failure. A result
#: starting with it means the handler did not change anything, which is how a
#: refused write is told apart from an applied one.
_ERROR_PREFIX = "Error:"

#: Why a revert left a file alone: it no longer says what the journal wrote.
#: Named so a caller that knows *what* changed it since — a later run, in
#: DR-061's one-file undo — can say so instead of this general reason.
CHANGED_SINCE = "it has changed since AutoSE wrote it"


@dataclass(frozen=True)
class FileChange:
    """One file, as it was and as it now is."""

    path: str
    #: ``None`` means the file did not exist. Bytes rather than text so a
    #: file that is not valid UTF-8 is still restorable exactly.
    before: bytes | None = None
    after: bytes | None = None
    #: Which tool made the change, for rendering and for the audit trail.
    tool: str = ""
    #: False when the content was too large to hold. The change is still
    #: reported; it simply cannot be undone or diffed.
    restorable: bool = True
    #: Why it is not restorable, when it is not.
    reason: str = ""

    @property
    def kind(self) -> str:
        if self.before is None:
            return "created"
        if self.after is None:
            return "deleted"
        return "modified"

    @property
    def is_text(self) -> bool:
        """Whether both sides decode as UTF-8 and hold no NUL byte."""
        return all(_is_text(side) for side in (self.before, self.after))

    @property
    def stats(self) -> tuple[int, int]:
        """``(lines added, lines removed)``. ``(0, 0)`` when undiffable."""
        if not self.restorable or not self.is_text:
            return (0, 0)
        added = removed = 0
        for line in self._diff_lines():
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return (added, removed)

    def diff(self, *, context: int = 3) -> str:
        """A unified diff of this change, or a one-line note when it has none."""
        if not self.restorable:
            return f"# {self.path}: {self.reason}"
        if not self.is_text:
            return f"# {self.path}: binary file {self.kind}"
        return "\n".join(self._diff_lines(context=context))

    def _diff_lines(self, *, context: int = 3) -> list[str]:
        before = _decode(self.before).splitlines(keepends=True)
        after = _decode(self.after).splitlines(keepends=True)
        lines = difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{self.path}" if self.before is not None else "/dev/null",
            tofile=f"b/{self.path}" if self.after is not None else "/dev/null",
            n=context,
        )
        # ``keepends`` preserves the trailing newline difflib needs to detect
        # a missing final newline; strip it back off for a line list.
        return [line.rstrip("\n") for line in lines]


@dataclass(frozen=True)
class RevertReport:
    """What an undo actually managed to do."""

    restored: tuple[str, ...] = ()
    #: ``(path, why)`` for each file deliberately left alone.
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.restored) and not self.skipped

    @property
    def empty(self) -> bool:
        return not self.restored and not self.skipped


class ChangeJournal:
    """Records what the mutating tools did to a workspace, and can undo it."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        max_tracked_files: int = DEFAULT_MAX_TRACKED_FILES,
    ) -> None:
        self._workspace = workspace
        self._max_snapshot_bytes = max_snapshot_bytes
        self._max_tracked_files = max_tracked_files
        #: Keyed by display path, in the order each file was first touched.
        self._changes: dict[str, FileChange] = {}
        #: Files that could not be tracked because the cap was reached.
        self.overflowed: int = 0

    @classmethod
    def from_changes(
        cls, workspace: Workspace, changes: tuple[FileChange, ...], **kwargs: Any
    ) -> ChangeJournal:
        """Rebuild a journal from changes that were carried on a finished turn.

        What makes undo work after the run that produced it has ended: a
        :class:`FileChange` holds both halves of the change, so the object
        that can revert it is reconstructible from the record rather than
        having to be kept alive.
        """
        journal = cls(workspace, **kwargs)
        journal._changes = {change.path: change for change in changes}
        return journal

    # -- introspection ---------------------------------------------------

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def changes(self) -> tuple[FileChange, ...]:
        return tuple(self._changes.values())

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self._changes)

    def __bool__(self) -> bool:
        return bool(self._changes)

    def __len__(self) -> int:
        return len(self._changes)

    def __iter__(self) -> Iterator[FileChange]:
        return iter(self._changes.values())

    def stats(self) -> tuple[int, int]:
        """Total ``(added, removed)`` line counts across every change."""
        added = removed = 0
        for change in self._changes.values():
            gained, lost = change.stats
            added += gained
            removed += lost
        return (added, removed)

    def diff(self, *, context: int = 3) -> str:
        """One unified diff covering every file this journal recorded."""
        blocks = [change.diff(context=context) for change in self._changes.values()]
        return "\n".join(block for block in blocks if block)

    def clear(self) -> None:
        self._changes.clear()
        self.overflowed = 0

    # -- recording -------------------------------------------------------

    def observe(self, handler: ToolHandler, *, tool: str = "") -> ToolHandler:
        """Wrap *handler* so every accepted change it makes is journalled.

        Composes rather than replaces: whatever the stage already installed —
        DR-018's decision-records confinement, a per-tier edit cap — still
        runs, and only its *accepted* writes are recorded. A refused write
        returns an ``Error:`` string, which is how a refusal is told apart
        from a change.
        """

        def wrapped(path: str = "", *args: Any, **kwargs: Any) -> str:
            if not path:
                return handler(path, *args, **kwargs)
            before, before_ok = self._snapshot(path)
            result = handler(path, *args, **kwargs)
            if isinstance(result, str) and result.startswith(_ERROR_PREFIX):
                return result
            after, after_ok = self._snapshot(path)
            self.record(
                path,
                before=before,
                after=after,
                tool=tool,
                restorable=before_ok and after_ok,
            )
            return result

        return wrapped

    def record(
        self,
        path: str,
        *,
        before: bytes | None,
        after: bytes | None,
        tool: str = "",
        restorable: bool = True,
    ) -> None:
        """Note one file's before/after, coalescing with anything already held."""
        display = self._display(path)
        if display is None:
            return
        existing = self._changes.get(display)
        if existing is None:
            if len(self._changes) >= self._max_tracked_files:
                self.overflowed += 1
                return
            if before == after and restorable:
                # A call that changed nothing is not a change. Recorded only
                # when the snapshot failed, where equality proves nothing.
                return
            self._changes[display] = FileChange(
                path=display,
                before=before,
                after=after,
                tool=tool,
                restorable=restorable,
                reason="" if restorable else self._too_large_reason(),
            )
            return

        # Coalesce: keep the *original* before, take the newest after, and let
        # one unrestorable snapshot poison the pair — a change whose halves
        # are not both held cannot be undone.
        combined_restorable = existing.restorable and restorable
        if existing.before == after and combined_restorable:
            # Edited back to exactly where it started: no net change remains.
            del self._changes[display]
            return
        self._changes[display] = FileChange(
            path=display,
            before=existing.before,
            after=after,
            tool=tool or existing.tool,
            restorable=combined_restorable,
            reason="" if combined_restorable else self._too_large_reason(),
        )

    # -- undo ---------------------------------------------------------------

    def revert(self) -> RevertReport:
        """Put every recorded file back, skipping any that moved on since.

        Skipping rather than overwriting is the whole safety property: between
        the run and the undo, a file may have been edited by the user, a
        formatter, or a later turn. Restoring a stale snapshot over that would
        destroy work AutoSE never made.
        """
        restored: list[str] = []
        skipped: list[tuple[str, str]] = []

        # Newest first, so a file created inside a directory this run created
        # is removed before anything tries to reason about the directory.
        for change in reversed(list(self._changes.values())):
            outcome = self._revert_one(change)
            if outcome is None:
                restored.append(change.path)
            else:
                skipped.append((change.path, outcome))

        if restored:
            self._changes = {
                path: change
                for path, change in self._changes.items()
                if path not in set(restored)
            }
        return RevertReport(restored=tuple(restored), skipped=tuple(skipped))

    def _revert_one(self, change: FileChange) -> str | None:
        """Restore one file. Returns ``None`` on success, else why it was skipped."""
        if not change.restorable:
            return change.reason or "its contents were too large to keep for undo"
        try:
            target = self._workspace.resolve(change.path)
        except WorkspaceEscapeError:
            return "it is no longer inside the workspace"

        current, current_ok = self._read(target)
        if not current_ok:
            return "it can no longer be read"
        if current != change.after:
            return CHANGED_SINCE

        try:
            if change.before is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(change.before)
        except OSError as exc:
            return f"it could not be written back ({exc})"
        return None

    # -- internals ------------------------------------------------------------

    def _display(self, path: str) -> str | None:
        try:
            return self._workspace.display(self._workspace.resolve(path))
        except WorkspaceEscapeError:
            return None

    def _snapshot(self, path: str) -> tuple[bytes | None, bool]:
        """Read *path* for the journal. Returns ``(content, held)``."""
        try:
            target = self._workspace.resolve(path)
        except WorkspaceEscapeError:
            return (None, False)
        return self._read(target)

    def _read(self, target: Path) -> tuple[bytes | None, bool]:
        try:
            if not target.exists():
                return (None, True)
            if not target.is_file():
                return (None, False)
            if target.stat().st_size > self._max_snapshot_bytes:
                return (None, False)
            return (target.read_bytes(), True)
        except OSError:
            return (None, False)

    def _too_large_reason(self) -> str:
        limit = self._max_snapshot_bytes // 1024
        return f"it is larger than the {limit} KiB AutoSE keeps for undo"


def track_changes(
    agent: Any,
    journal: ChangeJournal | None,
    *,
    tools: tuple[str, ...] = ("write_file", "edit_file"),
    on_change: Callable[[str], None] | None = None,
) -> None:
    """Journal every accepted write an agent makes.

    Instruments the agent's *own* cloned registry (DR-008), so the wrapper
    dies with the agent and no restoration step is needed. A ``None`` journal
    is a no-op, which is what lets a caller with nothing to record use the
    same code path.
    """
    if journal is None and on_change is None:
        return

    for name in tools:
        if not agent.has_tool(name):
            continue

        def wrap(handler: ToolHandler, tool: str = name) -> ToolHandler:
            recorded = (
                journal.observe(handler, tool=tool) if journal is not None else handler
            )
            if on_change is None:
                return recorded

            def notifying(path: str = "", *args: Any, **kwargs: Any) -> str:
                result = recorded(path, *args, **kwargs)
                if path and isinstance(result, str) and not result.startswith(
                    _ERROR_PREFIX
                ):
                    on_change(path)
                return result

            return notifying

        agent.instrument(name, wrap)


# -- helpers ------------------------------------------------------------------


def _decode(content: bytes | None) -> str:
    return "" if content is None else content.decode("utf-8", errors="replace")


def _is_text(content: bytes | None) -> bool:
    if content is None:
        return True
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
