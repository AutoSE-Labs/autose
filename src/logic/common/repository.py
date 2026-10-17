"""Repository facts `logic` needs for itself, not for a model.

The read-only git *tools* (DR-025) answer a model's questions and are shaped
for a model to read. This module answers AutoSE's own questions, and they are
different ones: what revision is checked out, what branch, and — the load
bearing one — is a given revision an ancestor of the current checkout.

That last question is what lets anything scoped to a revision stay safe
across branches: what holds on a feature branch is not necessarily true on
`main`, and reachability is the check that turns "somewhere in this
repository" into "somewhere this checkout actually descends from".

Everything here degrades to "unknown" rather than raising. A workspace that is
not a repository is an ordinary case for a local-first tool, and it must cost
a caller nothing more than a ``None``.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

from .workspace import Workspace

__all__ = [
    "run_git",
    "head_revision",
    "current_branch",
    "is_ancestor",
    "is_repository",
    "GIT_TIMEOUT_SECONDS",
]

#: Wall-clock ceiling for one git invocation. Generous enough for a large
#: repository's diff, short enough that a hung git does not hang a stage.
GIT_TIMEOUT_SECONDS = 30.0


def run_git(root, *arguments: str) -> tuple[bool, str]:
    """Run one git subcommand under *root*. Returns ``(ok, output_or_message)``.

    An argv list with ``shell=False``, always — every caller in this codebase
    passes at least one value that ultimately came from a model, and a shell
    would make that a command-injection surface rather than an argument.

    A tuple rather than a raised exception because a git failure is usually
    information: "this is not a repository" is an answer.
    """
    environment = dict(os.environ)
    # Never take the index lock: an inspection must not block, or be blocked
    # by, a git command the user is running in another terminal.
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    # A pager attached to a captured pipe hangs, and --no-pager alone is not
    # enough when a repository configures `core.pager` with a forced flag.
    environment["GIT_PAGER"] = "cat"
    environment["GIT_TERMINAL_PROMPT"] = "0"

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False
            # S607: `git` is resolved from PATH deliberately. AutoSE must use
            # the same git the user does — their version, their config, their
            # credential helpers — and pinning an absolute path would break
            # every environment that installs it somewhere else.
            ["git", "--no-pager", *arguments],  # noqa: S607
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
            check=False,
        )
    except FileNotFoundError:
        return (False, "Error: git is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        return (False, f"Error: git timed out after {GIT_TIMEOUT_SECONDS:.0f} seconds.")
    except OSError as exc:
        return (False, f"Error: could not run git: {exc}")

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return (False, stderr or f"git exited {completed.returncode}")
    return (True, completed.stdout)


def is_repository(workspace: Workspace) -> bool:
    ok, _ = run_git(workspace.root, "rev-parse", "--git-dir")
    return ok


def head_revision(workspace: Workspace) -> str | None:
    """The full commit hash of the current checkout, or ``None``.

    ``None`` covers both "not a repository" and "a repository with no commits
    yet" — neither is an error, and neither gives a revision to scope
    knowledge by.
    """
    ok, output = run_git(workspace.root, "rev-parse", "HEAD")
    revision = output.strip() if ok else ""
    return revision or None


def current_branch(workspace: Workspace) -> str | None:
    """The checked-out branch name, or ``None`` on a detached HEAD."""
    ok, output = run_git(workspace.root, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok:
        return None
    name = output.strip()
    return None if name in ("", "HEAD") else name


def is_ancestor(workspace: Workspace, revision: str) -> bool:
    """Whether *revision* is reachable from the current checkout.

    Answers "could what was learned at that revision still be true here?".
    Deliberately strict: an unknown revision, an unreadable repository, or a
    workspace with no git at all all answer ``False``, because DR-004 requires
    an unreachable entry be treated as *stale for this session* rather than
    applied hopefully.
    """
    if not revision or not _looks_like_a_revision(revision):
        return False
    ok, _ = run_git(
        workspace.root, "merge-base", "--is-ancestor", revision, "HEAD"
    )
    return ok


@lru_cache(maxsize=256)
def _looks_like_a_revision(value: str) -> bool:
    """Reject anything that is not a plain hex hash before it reaches git.

    A revision is the one place an option could hide in these calls, and every
    revision this module stores is one it generated itself from ``rev-parse``.
    Requiring hex costs nothing and removes the question.
    """
    return 4 <= len(value) <= 64 and all(
        character in "0123456789abcdefABCDEF" for character in value
    )
