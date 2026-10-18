"""Shell execution handler.

``run_command`` is approval-gated in the shared executor (DR-012); the checks
here are the second, independent layer: they keep an *approved* command from
reaching outside the workspace by way of an absolute path, a parent-directory
traversal, or a ``cd``. Approval answers "should this run at all"; these
checks answer "can it reach past the workspace even so".

**These checks are not a sandbox, and must not be described as one.** They
match patterns against the command string, so they catch the mistake a model
actually makes — reaching for ``/etc/...`` or ``~/`` out of habit — and they
do not survive deliberate evasion: a subshell, an expanded variable, or a
base64-decoded path all reach the shell unexamined. That is acceptable only
because approval is the real gate and a human has read the command. Anything
stronger needs OS-level confinement, which is a separate decision from this
one.
"""

from __future__ import annotations

import re
import subprocess

from ...errors import WorkspaceEscapeError
from ...limits import describe_limit, duration_limit
from ..workspace import Workspace

__all__ = ["run_command", "refuse_escaping_command", "COMMAND_TIMEOUT_SECONDS"]

#: Wall-clock ceiling for one command, when a repo's config does not set one
#: (DR-021). A hung build must not hang the stage.
#:
#: The one cap DR-030 deliberately leaves in place: while a command is running
#: nothing is checking the cancel token, so an unbounded ``run_command`` is the
#: single place a user cannot stop a run by asking. ``-1`` still turns it off,
#: for a repo whose test suite legitimately takes longer than this.
COMMAND_TIMEOUT_SECONDS = 120.0

_ABSOLUTE_PATH = re.compile(r"(^|[\s'\"=])/")
_PARENT_PATH = re.compile(r"(^|[\s'\"=])\.\.(?:/|\b)")
_DIRECTORY_CHANGE = re.compile(r"(^|[\s;&|])cd\s+")
#: ``~`` and ``$HOME``/``${HOME}`` both resolve outside the workspace root the
#: same way an absolute path does, and neither starts with a literal ``/``.
_HOME_PATH = re.compile(r"(^|[\s'\"=])~")
_HOME_VAR = re.compile(r"\$\{?HOME\}?\b")


def run_command(
    command: str,
    cwd: str = ".",
    *,
    workspace: Workspace,
    timeout: float | None = COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Run *command* inside the workspace and return combined output plus exit code.

    A *timeout* of ``-1``, ``0`` or ``None`` waits as long as the command takes
    (DR-030); ``subprocess`` reads that as ``None`` too, so the two agree.
    """
    limit = duration_limit(timeout)
    refusal = refuse_escaping_command(command)
    if refusal:
        return refusal
    try:
        resolved_cwd = workspace.resolve(cwd)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if not resolved_cwd.is_dir():
        return f"Error: working directory not found: {cwd}"

    try:
        completed = subprocess.run(  # noqa: S602 - shell is the point of this tool
            command,
            shell=True,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=limit,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {describe_limit(limit, 'seconds')}"
    except OSError as exc:
        return f"Error: could not run command: {exc}"

    parts: list[str] = []
    if completed.stdout:
        parts.append(completed.stdout)
    if completed.stderr:
        parts.append("STDERR:\n" + completed.stderr)
    parts.append(f"Exit code: {completed.returncode}")
    return "\n".join(parts).strip()


def refuse_escaping_command(command: str) -> str | None:
    """Why *command* reaches outside the workspace, or ``None`` if it does not.

    Public so the one other place AutoSE runs a shell command — a watcher the
    user defined (DR-047) — applies the same confinement rather than a copy.
    """
    if _ABSOLUTE_PATH.search(command):
        return (
            "Error: command references an absolute path. "
            "Use paths relative to the workspace root."
        )
    if _HOME_PATH.search(command) or _HOME_VAR.search(command):
        return (
            "Error: command references the home directory. "
            "Use paths relative to the workspace root."
        )
    if _PARENT_PATH.search(command):
        return (
            "Error: command references a parent path. "
            "Use paths relative to the workspace root."
        )
    if _DIRECTORY_CHANGE.search(command):
        return (
            "Error: command may not change directories; use the 'cwd' argument instead."
        )
    return None

