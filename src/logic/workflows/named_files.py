"""The files a plan names, read fresh from disk for the execute stage (DR-069).

Execute starts with a clean memory (DR-004): it gets the plan, not the files.
On qwen3:8b it then edited before reading anything, in every run measured:
paths it guessed (`src/calc.py` for `calc.py`) and text it invented, followed
by four to eight turns of listing, failing and re-reading before its first
real edit. The plan already says which files it means. Their current contents
are a fact on disk, not the plan's opinion of them, so handing them over keeps
each stage's memory clean while removing the guessing.
"""

from __future__ import annotations

import re

from ..common.tools.filesystem import read_file
from ..common.workspace import Workspace
from ..errors import WorkspaceEscapeError

__all__ = ["named_files", "named_files_section"]

#: At most this many files are included, in the order the plan names them.
MAX_FILES = 4

#: At most this many characters of file views in all. A file that would go
#: over it is left out whole, not cut, and Execute reads it itself.
MAX_CHARS = 12_000

#: A path-like token: no spaces, ends in an extension (`calc.py`,
#: `src/app/main.ts`). Anything it catches that is not a file is ignored.
_PATH_LIKE = re.compile(r"[\w./-]*\w\.\w+")

_HEADING = "# Current contents of the files the plan names"
_NOTE = (
    "Read from disk as this stage started. These are the real paths and the "
    "real text: edit against them."
)


def named_files(text: str, workspace: Workspace) -> list[str]:
    """Workspace files that *text* names, in the order it first names them."""
    found: list[str] = []
    for token in _PATH_LIKE.findall(text):
        try:
            target = workspace.resolve(token)
        except (WorkspaceEscapeError, OSError, ValueError):
            continue
        if not target.is_file():
            continue
        shown = workspace.display(target)
        if shown not in found:
            found.append(shown)
    return found


def named_files_section(text: str, workspace: Workspace) -> str:
    """The current contents of the files *text* names, or ``""`` if none."""
    views: list[str] = []
    used = 0
    for path in named_files(text, workspace):
        if len(views) == MAX_FILES:
            break
        view = read_file(path, workspace=workspace)
        if view.startswith("Error") or used + len(view) > MAX_CHARS:
            continue
        views.append(view)
        used += len(view)
    if not views:
        return ""
    return "\n\n".join([_HEADING, _NOTE, *views])
