"""Workspace root and path-boundary enforcement.

Every filesystem-touching tool resolves paths through a :class:`Workspace`, so
"can this call reach outside the directory the user pointed AutoSE at" is
answered in exactly one place rather than re-implemented per tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkspaceEscapeError

__all__ = ["Workspace", "WORKSPACE_ESCAPE_MESSAGE"]

WORKSPACE_ESCAPE_MESSAGE = (
    "path escapes the workspace root; use a path relative to the workspace."
)


@dataclass(frozen=True)
class Workspace:
    """An absolute, resolved directory that bounds all filesystem tool access."""

    root: Path

    @classmethod
    def at(cls, path: str | Path | None = None) -> Workspace:
        """Resolve *path* (default: the current directory) into a workspace."""
        base = Path.cwd() if path is None else Path(path).expanduser()
        return cls(root=base.resolve())

    def resolve(self, path: str | Path) -> Path:
        """Resolve *path* against the root, refusing anything that escapes it.

        Symlinks are followed during resolution, so a link pointing outside the
        workspace is rejected on the strength of its target, not its location.
        """
        candidate = Path(path)
        resolved = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (self.root / candidate).resolve(strict=False)
        )
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceEscapeError(WORKSPACE_ESCAPE_MESSAGE) from exc
        return resolved

    def display(self, path: Path) -> str:
        """Render *path* relative to the root for model- and user-facing text."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def contains(self, path: str | Path) -> bool:
        """Return whether *path* resolves inside this workspace."""
        try:
            self.resolve(path)
        except WorkspaceEscapeError:
            return False
        return True

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.root)
