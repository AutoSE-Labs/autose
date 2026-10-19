"""Reading the workspace for the person, not the model (DR-051).

The dashboard's Files view lists the workspace and shows a file's text. Both
go through the same boundary as every tool: paths resolve inside the
workspace root, a symlink is judged by its target, and the directories the
tools never walk are not listed either. Nothing here writes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .tools.filesystem import _SKIP_DIRECTORIES as SKIP_DIRECTORIES
from .workspace import Workspace

__all__ = ["FileText", "list_workspace", "read_workspace_file", "MAX_LISTED", "MAX_SHOWN_BYTES"]

#: A listing stops here; a workspace bigger than this is shown in part.
MAX_LISTED = 5000

#: A file's text is shown up to this size, and cut there beyond it.
MAX_SHOWN_BYTES = 1024 * 1024


def list_workspace(workspace: Workspace) -> tuple[list[dict[str, object]], bool]:
    """Every file under the root as ``{path, size}``, sorted, and whether the
    listing stopped at :data:`MAX_LISTED`."""
    root = workspace.root
    files: list[dict[str, object]] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRECTORIES)
        for name in sorted(filenames):
            path = Path(directory) / name
            if not workspace.contains(path):
                continue  # a link that leads outside
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append({"path": path.relative_to(root).as_posix(), "size": size})
            if len(files) >= MAX_LISTED:
                return files, True
    return files, False


@dataclass(frozen=True)
class FileText:
    path: str
    size: int
    text: str
    binary: bool
    truncated: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "text": self.text,
            "binary": self.binary,
            "truncated": self.truncated,
        }


def read_workspace_file(workspace: Workspace, path: str) -> FileText:
    """A file's text for showing. Raises ``WorkspaceEscapeError`` for a path
    outside the root, ``FileNotFoundError`` for one that is not a file, and
    ``PermissionError`` for a file inside a directory that is never listed."""
    resolved = workspace.resolve(path)
    relative = resolved.relative_to(workspace.root)
    if any(part in SKIP_DIRECTORIES for part in relative.parts[:-1]):
        raise PermissionError(f"{relative.as_posix()} is inside a folder the Files view does not show")
    if not resolved.is_file():
        raise FileNotFoundError(f"{relative.as_posix()} is not a file")
    size = resolved.stat().st_size
    with resolved.open("rb") as handle:
        data = handle.read(MAX_SHOWN_BYTES)
    if b"\0" in data[:8192]:
        return FileText(relative.as_posix(), size, "", True, False)
    return FileText(
        relative.as_posix(),
        size,
        data.decode("utf-8", errors="replace"),
        False,
        size > MAX_SHOWN_BYTES,
    )
