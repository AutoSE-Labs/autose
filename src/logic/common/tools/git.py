"""Read-only git inspection handlers (DR-025).

`git status`, `git diff` and `git log` are how an agent finds out what state
the working tree is actually in — the single most common reason a stage
reached for `run_command` and interrupted the user for approval. DR-012 gates
`run_command` because it runs *arbitrary* shell; these three run one fixed,
read-only subcommand each, so they are ordinary read-only tools and are not
gated.

Three properties make that claim hold rather than merely sound plausible:

* **No shell.** Every invocation is an argv list passed to ``subprocess.run``
  with ``shell=False``, so nothing in a model-supplied value can be a command
  substitution, a pipe, or a second command.
* **No model-supplied flags.** The only free-text argument any handler takes
  is a path, and it is resolved through :class:`Workspace` and placed after a
  ``--`` separator, so ``--upload-pack=…`` reaches git as a pathspec that does
  not match rather than as an option.
* **No revisions.** None of these handlers accepts a revision or a range.
  A revision is a place an option can hide (``--output=…`` behind a ``git
  log`` alias), and the questions the tier actually needs answered — what
  changed, what is uncommitted, what happened recently — are answerable
  without one.

Anything beyond these three subcommands stays with `run_command`, where a
human reads the exact command first.
"""

from __future__ import annotations

from ...errors import WorkspaceEscapeError
from ..repository import GIT_TIMEOUT_SECONDS, run_git
from ..workspace import Workspace

__all__ = [
    "git_status",
    "git_diff",
    "git_log",
    "GIT_TIMEOUT_SECONDS",
    "GIT_LOG_DEFAULT_COUNT",
    "GIT_LOG_MAX_COUNT",
]

GIT_LOG_DEFAULT_COUNT = 15
GIT_LOG_MAX_COUNT = 100

#: What a handler returns when the workspace is not a repository. Phrased as
#: information rather than failure: plenty of workspaces legitimately are not
#: git repositories, and a stage should move on rather than retry.
_NOT_A_REPOSITORY = (
    "Not a git repository: this workspace has no git history to inspect. "
    "Answer from the files themselves."
)


def git_status(*, workspace: Workspace) -> str:
    """Report the branch and every uncommitted change, porcelain-formatted."""
    ok, output = _git(workspace, "status", "--porcelain=v1", "--branch")
    if not ok:
        return output
    if not output.strip():
        return "Working tree clean, and no branch information reported."
    return _annotate_status(output)


def git_diff(
    path: str = "",
    staged: bool = False,
    *,
    workspace: Workspace,
) -> str:
    """Show uncommitted changes as a unified diff, optionally for one path."""
    arguments = ["diff"]
    if _truthy(staged):
        arguments.append("--staged")
    pathspec = _pathspec(path, workspace)
    if isinstance(pathspec, str):
        return pathspec
    ok, output = _git(workspace, *arguments, *pathspec)
    if not ok:
        return output
    if not output.strip():
        scope = f" in {path}" if path else ""
        state = "staged" if _truthy(staged) else "unstaged"
        return f"No {state} changes{scope}."
    return output


def git_log(
    max_count: int = GIT_LOG_DEFAULT_COUNT,
    path: str = "",
    *,
    workspace: Workspace,
) -> str:
    """List recent commits — hash, relative date, author, subject."""
    try:
        count = int(max_count)
    except (TypeError, ValueError):
        return f"Error: max_count must be an integer, got {max_count!r}"
    count = max(1, min(count, GIT_LOG_MAX_COUNT))

    pathspec = _pathspec(path, workspace)
    if isinstance(pathspec, str):
        return pathspec
    ok, output = _git(
        workspace,
        "log",
        f"--max-count={count}",
        "--date=short",
        "--pretty=format:%h  %ad  %an  %s",
        *pathspec,
    )
    if not ok:
        return output
    if not output.strip():
        scope = f" touching {path}" if path else ""
        return f"No commits{scope}."
    return output


# -- internals ---------------------------------------------------------------


def _pathspec(path: str, workspace: Workspace) -> list[str] | str:
    """Render *path* as a ``-- <relative path>`` pathspec, or explain why not.

    The separator is what makes this safe: after ``--`` git treats the value
    as a path even when it begins with a dash, so a model-supplied string can
    never become an option. Resolving through the workspace first keeps it
    from naming a file outside the root.
    """
    if not path:
        return []
    try:
        resolved = workspace.resolve(path)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    return ["--", workspace.display(resolved)]


def _git(workspace: Workspace, *arguments: str) -> tuple[bool, str]:
    """Run one read-only git subcommand, translating failures for a model.

    Shares its runner with :mod:`logic.common.repository` so the hardening —
    no shell, no index lock, no pager, no credential prompt — is defined once
    and cannot drift between AutoSE's own git calls and the model-facing ones.
    """
    ok, output = run_git(workspace.root, *arguments)
    if ok:
        return (True, output)
    if "not a git repository" in output.lower():
        return (False, _NOT_A_REPOSITORY)
    if output.startswith("Error:"):
        return (False, output)
    return (False, f"Error: git failed: {output or 'no detail'}")


def _annotate_status(output: str) -> str:
    """Expand porcelain status codes into words, keeping the codes visible.

    The two-letter codes are precise but easy for a small model to misread —
    ` M` and `M ` mean opposite things about the index. Naming both columns
    costs one line each and removes the guess.
    """
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith("##"):
            lines.append(f"On branch {line[2:].strip()}")
            continue
        if len(line) < 3:
            continue
        index, worktree, name = line[0], line[1], line[3:]
        parts = []
        if index != " " and index != "?":
            parts.append(f"staged {_CODES.get(index, index)}")
        if worktree != " " and worktree != "?":
            parts.append(f"unstaged {_CODES.get(worktree, worktree)}")
        if index == "?" or worktree == "?":
            parts.append("untracked")
        lines.append(f"  {line[:2]}  {name}  ({', '.join(parts) or 'unchanged'})")
    return "\n".join(lines)


_CODES = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
    "T": "type changed",
}


def _truthy(value: object) -> bool:
    """Coerce a model-supplied boolean, which arrives as a string often enough."""
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)
