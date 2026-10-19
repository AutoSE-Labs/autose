"""Background commands, as the model sees them (DR-062).

``run_command`` hands a command meant to keep running to the session's
:class:`~logic.common.processes.ProcessManager` and returns after a short look
at it; these handlers are the rest of that conversation — reading what it has
printed since, and stopping it.

Neither is approval-gated. Reading output runs nothing. Stopping can only
reach a process *this session* started, under a ``run_command`` call that was
itself approved, so it undoes something already allowed rather than doing
anything new: there is no argument through which a model could name another
process on the machine.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..processes import BackgroundProcess, ProcessError, ProcessManager
from ..workspace import Workspace

__all__ = [
    "start_in_background",
    "process_output",
    "stop_process",
    "FIRST_LOOK_SECONDS",
    "OUTPUT_DEFAULT_LINES",
    "OUTPUT_MAX_LINES",
]

#: How long ``run_command`` watches a command it started in the background
#: before returning: long enough for a server to print where it is listening,
#: or for a typo to fail, and short enough that the model is not left waiting.
FIRST_LOOK_SECONDS = 2.0

#: Lines of that first look handed back. The rest is a ``process_output`` away.
_FIRST_LOOK_LINES = 40

OUTPUT_DEFAULT_LINES = 50
OUTPUT_MAX_LINES = 400

_NO_MANAGER = (
    "Error: nothing in this run can keep a command running in the background, "
    "so there is no background process to read or stop."
)


def start_in_background(
    command: str,
    *,
    processes: ProcessManager,
    cwd: Path,
    shown_cwd: str = ".",
    settle: float | None = None,
) -> str:
    """Start *command* through the manager and report its first moments.

    *settle* defaults to :data:`FIRST_LOOK_SECONDS`, read when called.
    """
    settle = FIRST_LOOK_SECONDS if settle is None else settle
    try:
        process = processes.start(command, cwd=cwd, shown_cwd=shown_cwd)
    except ProcessError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: could not start the command: {exc}"
    exited = process.wait(settle)
    seen = process.tail(_FIRST_LOOK_LINES)
    shown = "\n".join(seen) if seen else "(no output yet)"
    head = f"Started in the background as process {process.id}: {command}"
    if exited and not process.alive:
        return f"{head}\nIt ended within {settle:g} s: {_said(process)}. Its output:\n{shown}"
    state = (
        f"After {settle:g} s it {_said(process)}"
        if exited
        else f"Still running after {settle:g} s"
    )
    return (
        f"{head}\n{state}. Its output so far:\n{shown}\n\n"
        f'Read more later with process_output(process_id="{process.id}"); stop it '
        f'with stop_process(process_id="{process.id}"). It is stopped when this '
        "session ends."
    )


def process_output(
    process_id: str = "",
    lines: int = OUTPUT_DEFAULT_LINES,
    *,
    workspace: Workspace,
    processes: ProcessManager | None = None,
) -> str:
    """The latest output of one background process, or a list of them all."""
    if processes is None:
        return _NO_MANAGER
    if not str(process_id or "").strip():
        return _listing(processes)
    try:
        process = processes.get(process_id)
    except ProcessError as exc:
        return f"Error: {exc}. {_listing(processes)}"
    count = _clamp(lines)
    tail = process.tail(count)
    total = process.line_count
    head = (
        f"Process {process.id}: {process.command}\n"
        f"Status: {_said(process)} · started {_ago(process.started_at)} · "
        f"{total:,} line{'s' if total != 1 else ''} of output"
    )
    if not tail:
        return f"{head}\n(no output yet)"
    shown = f"last {len(tail)}" if total > len(tail) else "all"
    return f"{head}\nOutput ({shown}):\n" + "\n".join(tail)


def stop_process(
    process_id: str,
    *,
    workspace: Workspace,
    processes: ProcessManager | None = None,
) -> str:
    """Stop one background process this session started, and all it started."""
    if processes is None:
        return _NO_MANAGER
    try:
        process = processes.get(process_id)
    except ProcessError as exc:
        return f"Error: {exc}. {_listing(processes)}"
    if not process.alive:
        return f"Process {process.id} has already ended ({_said(process)}); nothing to stop."
    processes.stop(process.id)
    tail = process.tail(10)
    last = "\nIts last lines:\n" + "\n".join(tail) if tail else ""
    return f"Stopped process {process.id} ({process.command}).{last}"


def _listing(processes: ProcessManager) -> str:
    listed = processes.processes()
    if not listed:
        return "No command has been started in the background in this session."
    rows = [
        f"- {process.id} · {_said(process)} · {process.command}" for process in listed
    ]
    return "Background processes in this session:\n" + "\n".join(rows)


def _said(process: BackgroundProcess) -> str:
    """Its status as a phrase: "running", "exited with code 1", "stopped"."""
    status = process.status
    if status == "exited":
        # The command ended, but something it started still holds its output.
        left = ", but what it started is still running" if process.alive else ""
        return f"exited with code {process.exit_code}{left}"
    return status


def _clamp(lines: object) -> int:
    try:
        count = int(str(lines).strip()) if lines not in (None, "") else OUTPUT_DEFAULT_LINES
    except ValueError:
        count = OUTPUT_DEFAULT_LINES
    return max(1, min(count, OUTPUT_MAX_LINES))


def _ago(started_at: float) -> str:
    seconds = max(0, int(time.time() - started_at))
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min ago"
