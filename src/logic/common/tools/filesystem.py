"""Read-only filesystem tool handlers, plus whole-file write.

Every handler takes the workspace as a keyword argument injected by the
executor, resolves paths through it, and returns a plain string — errors
included. A tool result is *information for the model*, not an exception:
returning "Error: file not found" lets the agent correct itself, whereas
raising would abort a stage over a typo.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from ...errors import WorkspaceEscapeError
from ...limits import count_limit
from ..workspace import Workspace

__all__ = [
    "read_file",
    "list_files",
    "search_files",
    "find_files",
    "write_file",
    "READ_FILE_DEFAULT_MAX_LINES",
    "READ_FILE_DEFAULT_MAX_CHARS",
]

#: Lines returned by an unbounded ``read_file`` before it truncates and tells
#: the model to ask for a specific range.
READ_FILE_DEFAULT_MAX_LINES = 150

#: Characters returned by one ``read_file`` call before it truncates. Bounds
#: the axis `READ_FILE_DEFAULT_MAX_LINES` cannot: 150 lines of prose or
#: minified JSON is many times 150 lines of code. Without it an oversized
#: result is truncated a *second* time by `BoundedToolOutput` (DR-011), and
#: the model is handed two different truncation notices naming two different
#: recovery routes for one call — which a small local model answers by
#: re-issuing the identical read. Callers that know the real message budget
#: (the workflows do) inject a value derived from it; this default is only
#: the floor for a direct call.
READ_FILE_DEFAULT_MAX_CHARS = 3000

#: Directories never worth walking, and expensive to walk by accident.
#:
#: The build-tool caches matter as much as `.git` here: a repo that has run
#: mypy, pytest and ruff carries hundreds of hash-named cache files, and a
#: recursive listing that includes them spends the model's whole context
#: window on paths that say nothing about the project.
_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".autose",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".ipynb_checkpoints",
        ".gradle",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".turbo",
        ".eggs",
        "site-packages",
    }
)


def read_file(
    path: str,
    start_line: int = 1,
    end_line: int = -1,
    *,
    workspace: Workspace,
    max_chars: int | None = READ_FILE_DEFAULT_MAX_CHARS,
) -> str:
    """Return a bounded, line-numbered-range view of a text file.

    Bounded on both axes — lines and characters — so the result stays inside
    the tool-output cap and carries exactly one truncation notice. That notice
    names the next call to make rather than describing the parameters, because
    the model that most needs it is the one least likely to compose the call
    itself.
    """
    try:
        target = workspace.resolve(path)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if not target.exists():
        return f"Error: file not found: {path}"
    if not target.is_file():
        return f"Error: not a file: {path}"

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    low = max(1, int(start_line)) - 1
    explicit_end = int(end_line) != -1
    # What the caller asked for, before either bound narrows it. Truncation is
    # reported against *this*, so an explicit `end_line` that was honoured in
    # full is never announced as truncated.
    requested_end = min(int(end_line), total) if explicit_end else total
    high = requested_end
    if not explicit_end and (high - low) > READ_FILE_DEFAULT_MAX_LINES:
        high = low + READ_FILE_DEFAULT_MAX_LINES

    body = _clip_lines(lines[low:high], count_limit(max_chars, minimum=200))
    shown_end = low + len(body)

    header = f"# {workspace.display(target)}  (lines {low + 1}-{shown_end} of {total})\n"
    suffix = (
        f"\n\n[Truncated: showing lines {low + 1}-{shown_end} of {total}. "
        f"Continue with read_file(path={workspace.display(target)!r}, "
        f"start_line={shown_end + 1}).]"
        if shown_end < requested_end
        else ""
    )
    return header + "\n".join(body) + suffix


def _clip_lines(lines: list[str], limit: int | None) -> list[str]:
    """Return the leading whole lines of *lines* that fit in *limit* chars.

    Always returns at least one line — hard-sliced when a single line is
    longer than the whole budget — so a file of one enormous minified line
    returns its beginning rather than nothing at all.
    """
    if limit is None or not lines:
        return list(lines)
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > limit:
            if not kept:
                return [line[:limit]]
            break
        kept.append(line)
        used += cost
    return kept


def list_files(
    directory: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    *,
    workspace: Workspace,
) -> str:
    """List entries in a directory, optionally recursing."""
    try:
        root = workspace.resolve(directory)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if not root.exists():
        return f"Error: directory not found: {directory}"
    if not root.is_dir():
        return f"Error: not a directory: {directory}"

    if recursive:
        matches = sorted(
            path
            for path in root.rglob(pattern)
            if path.is_file() and not _is_skipped(path, root)
        )
    else:
        # Skipped directories are filtered here as well as in the recursive
        # branch: a plain listing of a repository root that shows `.mypy_cache`
        # and `.ruff_cache` alongside `src` misdirects the model just as
        # effectively, it simply costs fewer tokens to do it.
        matches = sorted(
            path
            for path in root.glob(pattern)
            if path.name not in _SKIP_DIRECTORIES
        )
    if not matches:
        return f"No files matching '{pattern}' in {directory}"
    return "\n".join(workspace.display(path) for path in matches)


def search_files(
    text: str,
    directory: str = ".",
    file_pattern: str = "*",
    max_results: int = 50,
    *,
    workspace: Workspace,
) -> str:
    """Return matching lines for a regex across files, with path and line number."""
    try:
        root = workspace.resolve(directory)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if not root.exists():
        return f"Error: directory not found: {directory}"
    try:
        regex = re.compile(text, re.IGNORECASE)
    except re.error as exc:
        return f"Error: invalid pattern: {exc}"

    cap = max(1, int(max_results))
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRECTORIES]
        for filename in sorted(filenames):
            if not fnmatch.fnmatch(filename, file_pattern):
                continue
            filepath = Path(dirpath) / filename
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if not regex.search(line):
                    continue
                results.append(
                    f"{workspace.display(filepath)}:{number}: {line.rstrip()}"
                )
                if len(results) >= cap:
                    results.append(f"... (results capped at {cap})")
                    return "\n".join(results)
    return "\n".join(results) if results else f"No matches for '{text}' in {directory}"


def find_files(
    name_pattern: str,
    directory: str = ".",
    *,
    workspace: Workspace,
) -> str:
    """Find files whose name matches a glob, anywhere beneath a directory."""
    try:
        root = workspace.resolve(directory)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if not root.exists():
        return f"Error: directory not found: {directory}"
    matches = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and fnmatch.fnmatch(path.name, name_pattern)
        and not _is_skipped(path, root)
    )
    if not matches:
        return f"No files matching '{name_pattern}' under {directory}"
    return "\n".join(workspace.display(path) for path in matches)


def write_file(path: str, content: str, *, workspace: Workspace) -> str:
    """Create a file, or replace one wholesale, with *content*."""
    try:
        target = workspace.resolve(path)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if target.exists() and not target.is_file():
        return f"Error: not a file: {path}"
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the content byte-exact; otherwise Windows turns each
    # LF into CRLF, unlike edit_file, which preserves line endings.
    target.write_text(content, encoding="utf-8", newline="")
    return f"Wrote {len(content.splitlines())} line(s) to {workspace.display(target)}"


def _is_skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return any(part in _SKIP_DIRECTORIES for part in parts)
