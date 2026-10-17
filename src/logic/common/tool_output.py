"""Bounded-but-recoverable tool output (DR-011).

Tool results have to be capped or they swamp the context window. Capping by
keeping the first N characters is the wrong cap: compiler, linter, and test
output puts the decisive summary at the *end*, which front-truncation throws
away exactly when it matters.

So an oversized result is instead:

1. written in full to a content-addressed file under a workspace-local,
   git-ignored directory (identical calls therefore deduplicate);
2. returned to the model as a head **and** tail preview carrying an explicit
   notice and the artifact's workspace-relative path, recoverable in bounded
   ranges through the ordinary ``read_file`` tool — no new tool schema;
3. rotated by recency, on a best-effort basis, to a bounded artifact count.

Persistence never fails the underlying tool call: if the disk write cannot
happen, the preview is still returned, with a notice saying so.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..limits import count_limit
from .workspace import Workspace

__all__ = [
    "ToolOutputStore",
    "FileToolOutputStore",
    "NullToolOutputStore",
    "BoundedToolOutput",
    "DEFAULT_MAX_TOOL_OUTPUT_CHARS",
    "TOOL_OUTPUT_DIRECTORY",
]

#: Characters of tool result the model sees before bounding kicks in. ``-1``
#: passes every result through whole (DR-030), which is a context-window
#: decision rather than a "how long may AutoSE work" one: the model that most
#: needs a bound is the small local one this cap was sized for.
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 4000

#: Workspace-relative home for preserved output. Kept under ``.autose`` so a
#: single gitignore entry covers every artifact kind AutoSE writes.
TOOL_OUTPUT_DIRECTORY = ".autose/tool-output"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@runtime_checkable
class ToolOutputStore(Protocol):
    """Strategy seam: where complete oversized output is preserved.

    The filesystem implementation is the default; a hosted deployment could
    register an object-store implementation without touching the executor.
    """

    def save(self, tool_name: str, arguments: Mapping[str, Any], result: str) -> str | None:
        """Persist *result*, returning a recovery location, or ``None`` on failure."""


class NullToolOutputStore:
    """Preserves nothing. Oversized output is still previewed head-and-tail."""

    def save(  # Protocol implementation.
        self, tool_name: str, arguments: Mapping[str, Any], result: str
    ) -> str | None:
        return None


class FileToolOutputStore:
    """Content-addressed artifact files inside the workspace."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        directory: str = TOOL_OUTPUT_DIRECTORY,
        max_artifacts: int | None = 20,
    ) -> None:
        self._workspace = workspace
        self._directory = directory
        #: ``None`` keeps every artifact — rotation is then skipped entirely
        #: rather than run with an impossible target (DR-030).
        self._max_artifacts = count_limit(max_artifacts)

    def save(  # Protocol implementation.
        self, tool_name: str, arguments: Mapping[str, Any], result: str
    ) -> str | None:
        try:
            directory = self._prepare_directory()
            artifact = directory / self._artifact_name(tool_name, arguments, result)
            if artifact.is_symlink():
                # Never follow a planted symlink out of the workspace.
                return None
            if not artifact.exists():
                self._write_new(artifact, result)
            elif not artifact.is_file():
                return None
            self._rotate(directory, keep=artifact)
            return self._workspace.display(artifact)
        except (OSError, ValueError, TypeError):
            # DR-011: persistence failure must never fail the tool call.
            return None

    # -- internals -----------------------------------------------------

    def _prepare_directory(self) -> Path:
        directory = self._workspace.resolve(self._directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._ensure_ignored(directory)
        return directory

    @staticmethod
    def _ensure_ignored(directory: Path) -> None:
        """Keep preserved output out of the user's git history, best effort.

        The marker goes in ``.autose/`` (the parent) so it covers every
        artifact directory AutoSE creates, not just this one.
        """
        marker = directory.parent / ".gitignore"
        if marker.exists():
            return
        try:
            marker.write_text("*\n", encoding="utf-8")
        except OSError:
            return

    @staticmethod
    def _artifact_name(
        tool_name: str, arguments: Mapping[str, Any], result: str
    ) -> str:
        payload = json.dumps(
            {"tool": tool_name, "arguments": dict(arguments), "result": result},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()[:16]
        safe = _UNSAFE_NAME.sub("-", tool_name).strip("-.") or "tool"
        return f"{safe}-{digest}.txt"

    @staticmethod
    def _write_new(artifact: Path, result: str) -> None:
        """Create the artifact owner-readable only, refusing to clobber."""
        try:
            descriptor = os.open(
                artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            # A concurrent run wrote the same content-addressed name; identical
            # content means there is nothing left to do.
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(result)

    def _rotate(self, directory: Path, *, keep: Path) -> None:
        if self._max_artifacts is None:
            return
        try:
            others = [
                path
                for path in directory.glob("*.txt")
                if path != keep and path.is_file() and not path.is_symlink()
            ]
            others.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
            for stale in others[max(0, self._max_artifacts - 1) :]:
                stale.unlink(missing_ok=True)
        except OSError:
            # Rotation is housekeeping; it must never mask a tool result.
            return


class BoundedToolOutput:
    """Applies the character cap, preserving the full result where possible."""

    def __init__(
        self,
        store: ToolOutputStore | None = None,
        *,
        max_chars: int | None = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
    ) -> None:
        self._store = store or NullToolOutputStore()
        #: ``None`` means no cap: `apply` then returns every result untouched
        #: and nothing is ever written to the store (DR-030).
        self._max_chars = count_limit(max_chars)

    @property
    def max_chars(self) -> int | None:
        return self._max_chars

    def clamped(self, max_chars: int | None) -> BoundedToolOutput:
        """Return this bound, narrowed to *max_chars* if that is tighter.

        A configured cap says how much output is worth reading; the window
        says how much can actually be carried between rounds. The smaller of
        the two is the only one that holds, and a cap that loses that argument
        silently is how a bounded result still overflows the budget (DR-010).

        Returns ``self`` when nothing narrows, so the ordinary case allocates
        nothing and the caller's own store is reused rather than rebuilt.
        """
        if max_chars is None:
            return self
        narrowed = count_limit(max_chars, minimum=1)
        if narrowed is None:
            return self
        if self._max_chars is not None and self._max_chars <= narrowed:
            return self
        return BoundedToolOutput(self._store, max_chars=narrowed)

    def apply(
        self, tool_name: str, arguments: Mapping[str, Any], result: str
    ) -> tuple[str, str | None]:
        """Return ``(model_facing_result, recovery_location_or_None)``."""
        if self._max_chars is None or len(result) <= self._max_chars:
            return result, None
        location = self._store.save(tool_name, arguments, result)
        return self._preview(result, location), location

    def _preview(self, result: str, location: str | None) -> str:
        assert self._max_chars is not None, "only a capped output is previewed"
        if location:
            marker = (
                f"[Output truncated: {len(result)} chars total; showing the beginning "
                f"and the end. Full output saved to {location} — read ranges of it "
                "with read_file.]"
            )
        else:
            marker = (
                f"[Output truncated: {len(result)} chars total; showing the beginning "
                "and the end. The full output could not be saved.]"
            )
        separator = 4  # two blank-line separators around the marker
        budget = max(0, self._max_chars - len(marker) - separator)
        head_size = (budget + 1) // 2
        tail_size = budget - head_size
        head = result[:head_size]
        tail = result[-tail_size:] if tail_size else ""
        return f"{head}\n\n{marker}\n\n{tail}"
