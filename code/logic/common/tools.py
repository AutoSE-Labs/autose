"""Shared tool implementations used across lite and standard agents."""

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Final

_READ_FILE_DEFAULT_MAX_LINES = 150
_WORKSPACE_ESCAPE_ERROR: Final[str] = (
    "Error: path escapes workspace root; use a path relative to the workspace."
)


def _workspace_path(workspace_root: str | Path | None) -> Path:
    if workspace_root is None:
        return Path.cwd().resolve()
    return Path(workspace_root).resolve()


def _resolve_path(path: str, workspace_root: str | Path | None) -> Path:
    workspace = _workspace_path(workspace_root)
    candidate = Path(path)
    resolved = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (workspace / candidate).resolve(strict=False)
    )
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(_WORKSPACE_ESCAPE_ERROR) from exc
    return resolved


def _display_path(path: Path, workspace_root: str | Path | None) -> str:
    workspace = _workspace_path(workspace_root)
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        return str(path)


def _validate_command(command: str) -> str | None:
    if re.search(r"(^|[\s'\"=])/(?![/*])", command):
        return (
            "Error: command references an absolute path. "
            "Use paths relative to the workspace root."
        )
    if re.search(r"(^|[\s'\"=])\.\.(?:/|\b)", command):
        return (
            "Error: command references a parent path. "
            "Use paths relative to the workspace root."
        )
    if re.search(r"(^|[\s;&|])cd\s+", command):
        return "Error: command may not change directories; use the cwd argument instead."
    return None


def _read_file(
    path: str,
    start_line: int = 1,
    end_line: int = -1,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    try:
        p = _resolve_path(path, workspace_root)
    except ValueError as exc:
        return str(exc)
    if not p.exists():
        return f"Error: file not found: {path}"
    if not p.is_file():
        return f"Error: not a file: {path}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    lo = max(1, start_line) - 1
    hi = total if end_line == -1 else min(end_line, total)
    # Cap default reads to avoid blowing the context window
    if end_line == -1 and (hi - lo) > _READ_FILE_DEFAULT_MAX_LINES:
        hi = lo + _READ_FILE_DEFAULT_MAX_LINES
    selected = lines[lo:hi]
    header = (
        f"# {_display_path(p, workspace_root)}  (lines {lo + 1}–{hi} of {total})\n"
    )
    suffix = (
        f"\n\n[Truncated: showing lines {lo + 1}–{hi} of {total}. Use start_line/end_line to read more.]"
        if hi < total and end_line == -1
        else ""
    )
    return header + "\n".join(selected) + suffix


def _list_files(
    directory: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    try:
        root = _resolve_path(directory, workspace_root)
    except ValueError as exc:
        return str(exc)
    if not root.exists():
        return f"Error: directory not found: {directory}"
    if not root.is_dir():
        return f"Error: not a directory: {directory}"
    if recursive:
        matches = sorted(p for p in root.rglob(pattern) if p.is_file())
    else:
        matches = sorted(root.glob(pattern))
    if not matches:
        return f"No files matching '{pattern}' in {directory}"
    return "\n".join(_display_path(p, workspace_root) for p in matches)


def _search_files(
    text: str,
    directory: str = ".",
    file_pattern: str = "*",
    max_results: int = 50,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    try:
        root = _resolve_path(directory, workspace_root)
    except ValueError as exc:
        return str(exc)
    if not root.exists():
        return f"Error: directory not found: {directory}"
    results: list[str] = []
    try:
        regex = re.compile(text, re.IGNORECASE)
    except re.error as exc:
        return f"Error: invalid pattern: {exc}"
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if not fnmatch.fnmatch(filename, file_pattern):
                continue
            filepath = Path(dirpath) / filename
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    results.append(
                        f"{_display_path(filepath, workspace_root)}:{i}: {line.rstrip()}"
                    )
                    if len(results) >= max_results:
                        results.append(f"... (results capped at {max_results})")
                        return "\n".join(results)
    return "\n".join(results) if results else f"No matches for '{text}' in {directory}"


def _find_files(
    name_pattern: str,
    directory: str = ".",
    *,
    workspace_root: str | Path | None = None,
) -> str:
    try:
        root = _resolve_path(directory, workspace_root)
    except ValueError as exc:
        return str(exc)
    if not root.exists():
        return f"Error: directory not found: {directory}"
    matches = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and fnmatch.fnmatch(p.name, name_pattern)
    )
    if not matches:
        return f"No files matching '{name_pattern}' under {directory}"
    return "\n".join(_display_path(p, workspace_root) for p in matches)


def _write_file(
    path: str,
    content: str,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    try:
        p = _resolve_path(path, workspace_root)
    except ValueError as exc:
        return str(exc)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    lines = len(content.splitlines())
    return f"Wrote {lines} line(s) to {_display_path(p, workspace_root)}"


def _edit_file(
    path: str,
    old_str: str,
    new_str: str,
    *,
    workspace_root: str | Path | None = None,
) -> str:
    try:
        p = _resolve_path(path, workspace_root)
    except ValueError as exc:
        return str(exc)
    if not p.exists():
        return f"Error: file not found: {path}"
    if not p.is_file():
        return f"Error: not a file: {path}"
    original = p.read_text(encoding="utf-8")
    count = original.count(old_str)
    if count == 0:
        return f"Error: old_str not found in {path}"
    if count > 1:
        return f"Error: old_str appears {count} times in {path}; make it more specific"
    p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
    return f"Successfully edited {_display_path(p, workspace_root)}"


def _run_command(
    command: str,
    cwd: str = ".",
    *,
    workspace_root: str | Path | None = None,
) -> str:
    """Run a shell command in the given directory and return combined stdout/stderr."""
    validation_error = _validate_command(command)
    if validation_error:
        return validation_error
    try:
        resolved_cwd = _resolve_path(cwd, workspace_root)
    except ValueError as exc:
        return str(exc)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"
    except OSError as exc:
        return f"Error: could not run command: {exc}"

    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append("STDERR:\n" + result.stderr)
    parts.append(f"Exit code: {result.returncode}")
    return "\n".join(parts).strip()
