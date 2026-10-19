"""Commands that keep running after the call that started them (DR-062).

``run_command`` runs a command to completion and returns what it printed. A
command meant to keep running — a dev server, a file watcher, anything ended
with ``&`` — never completes in that sense: the child it leaves behind holds
the output pipe open, so the call waits out its whole timeout, and whatever
was started is then orphaned with nobody able to see or stop it.

A :class:`ProcessManager` is where such a command lives instead. Each one
starts in its own process group inside the workspace; its combined output is
kept as a bounded tail that can be read again from a cursor; and it can be
stopped — SIGTERM to the whole group, SIGKILL after a short grace. One manager
belongs to one session, and stopping everything it holds is part of closing
that session, so nothing a run started outlives the session, the dashboard or
the terminal app. An ``atexit`` hook is the backstop for an exit that skips
the orderly close.

What happens is told as events on the session's sink — ``process_started``,
``process_exited``, ``process_ended`` (what an exited command left running has
gone too) and ``process_cleared`` — so an Adapter can keep a list of
terminals up to date without asking. Output itself is not an event: a chatty
server would otherwise flood every Adapter's event log, so it is read on
demand from the cursor instead.
"""

from __future__ import annotations

import atexit
import codecs
import contextlib
import io
import itertools
import os
import re
import signal
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..errors import AutoSEError
from .events import Event, EventSink, NullEventSink

__all__ = [
    "BackgroundProcess",
    "OutputSlice",
    "ProcessError",
    "ProcessManager",
    "UnknownProcessError",
    "background_form",
    "stop_every_process",
    "stop_managers",
    "exit_cleanly_on_termination",
    "MAX_OUTPUT_LINES",
    "MAX_RUNNING_PER_SESSION",
    "STOP_GRACE_SECONDS",
    "RUNNING",
    "EXITED",
    "STOPPED",
    "SUPPORTED",
]

#: Lines of output kept per process. Enough to scroll back through a server's
#: start-up and its last few hundred requests; past it the oldest go first.
MAX_OUTPUT_LINES = 2_000

#: How long a stopped process gets to exit on SIGTERM before SIGKILL.
STOP_GRACE_SECONDS = 2.0

#: Background commands one session may have running at once. A model that
#: starts its server again on every round instead of reading the first one's
#: output would otherwise fill the machine with copies of it.
MAX_RUNNING_PER_SESSION = 8

#: Whether this system can keep a command running here at all. Stopping one
#: with everything it started takes a process group of its own, and process
#: groups (``start_new_session``, ``os.killpg``) are POSIX: elsewhere a
#: background command is refused rather than started with no way to stop it.
SUPPORTED = os.name == "posix"

#: One line of output kept at most, in characters. A minified bundle printed
#: whole is one "line" of megabytes.
_MAX_LINE_CHARS = 4_000

_READ_CHUNK = 65_536

#: How long an exit waits for the last of the output. A grandchild that still
#: holds the pipe must not hold up telling anyone the command itself ended.
_DRAIN_SECONDS = 0.5

RUNNING = "running"
EXITED = "exited"
STOPPED = "stopped"

#: A trailing ``&`` on its own: not ``&&``, not the ``&`` of ``2>&1`` or
#: ``&>``, not an escaped ``\\&``.
_TRAILING_AMPERSAND = re.compile(r"(?<![&>|\\])&\s*$")
_LEADING_NOHUP = re.compile(r"^\s*nohup\s+")

_ids = itertools.count(1)
_ids_lock = threading.Lock()


class ProcessError(AutoSEError):
    """A background command cannot be started, stopped or cleared as asked."""


class UnknownProcessError(ProcessError):
    """No background process has that id here."""


def background_form(command: str) -> str | None:
    """The command to keep running, when *command* asks to run in the background.

    That is a command ending in a lone ``&``, optionally started with
    ``nohup``. The manager provides both of those itself — its own process
    group, output captured instead of a terminal — so what it runs, and what
    an allowance judges (DR-048), is the command without them. ``None`` when
    *command* is an ordinary one. Only the *trailing* ``&`` goes: ``a & b``
    is still two commands, and stays once-only.
    """
    if not _TRAILING_AMPERSAND.search(command):
        return None
    stripped = _TRAILING_AMPERSAND.sub("", command).rstrip()
    stripped = _LEADING_NOHUP.sub("", stripped, count=1).strip()
    return stripped or None


def _next_id() -> str:
    with _ids_lock:
        return f"p{next(_ids)}"


@dataclass(frozen=True)
class OutputSlice:
    """Lines of a process's output from a cursor onwards."""

    lines: tuple[str, ...]
    #: The number of the first line in ``lines`` (0-based, counted from the
    #: start of the process, not of what is kept).
    start: int
    #: Pass this as ``since`` next time to read only what is new.
    cursor: int
    #: Lines after the requested cursor that had already fallen out of the
    #: kept tail, so a reader can say some were missed.
    skipped: int
    #: Text after the last newline — a prompt, or a progress bar still being
    #: drawn. Not counted in ``cursor``: it may yet grow.
    partial: str = ""


class BackgroundProcess:
    """One command started by a :class:`ProcessManager`."""

    def __init__(
        self,
        command: str,
        *,
        cwd: Path,
        shown_cwd: str = ".",
        max_lines: int = MAX_OUTPUT_LINES,
    ) -> None:
        self.id = _next_id()
        self.command = command
        self.cwd = cwd
        #: The working directory as the command named it, for showing.
        self.shown_cwd = shown_cwd or "."
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self._status = RUNNING
        self._lines: deque[str] = deque(maxlen=max(1, max_lines))
        self._total = 0
        self._partial = ""
        self._lock = threading.Lock()
        self._popen: subprocess.Popen[bytes] | None = None
        self._stop_requested = False
        #: Set once the command has exited and everyone has been told.
        self._exited = threading.Event()
        #: Set once the output pipe has closed: nothing in the group holds it.
        self._drained = threading.Event()
        #: Whether, when its exit was told, something it left was still alive;
        #: then ``process_ended`` is owed once that has gone too.
        self._left_running = False
        self._end_told = False
        self._on_end = None

    # -- what it is --------------------------------------------------------

    @property
    def pid(self) -> int | None:
        return self._popen.pid if self._popen is not None else None

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    @property
    def alive(self) -> bool:
        """Whether anything it started may still be running.

        The command itself, or something it left behind in its group that
        still holds its output open — ``./start.sh`` that runs ``node
        server.js &`` and exits has "exited 0" while the server it started
        goes on printing. Such a process can still be stopped, and is not
        cleared from the list until it has been, or nothing could stop it.
        Only while its group still has a member: a pipe held by something
        that left the group (``setsid``) is beyond a stop's reach, so it is
        not offered one.
        """
        with self._lock:
            return self._alive()

    def _alive(self) -> bool:
        if self._status == RUNNING:
            return True
        if self._popen is None or self._drained.is_set():
            return False
        return self._group_exists()

    def _group_exists(self) -> bool:
        pid = self.pid
        if pid is None:
            return False
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @property
    def line_count(self) -> int:
        with self._lock:
            return self._total

    def describe(self) -> str:
        """Its status in a few words: "running", "exited 1", "stopped"."""
        status = self.status
        if status == EXITED:
            left = ", what it started still runs" if self.alive else ""
            return f"exited {self.exit_code}{left}"
        return status

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "command": self.command,
                "cwd": self.shown_cwd,
                "pid": self.pid,
                "status": self._status,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "lines": self._total,
                "alive": self._alive(),
            }

    # -- output ------------------------------------------------------------

    def output(self, since: int = 0) -> OutputSlice:
        """Every kept line after *since*, and the cursor to read on from."""
        with self._lock:
            total = self._total
            first = total - len(self._lines)
            since = min(max(0, since), total)
            start = max(since, first)
            lines = tuple(itertools.islice(self._lines, start - first, None))
            return OutputSlice(
                lines=lines,
                start=start,
                cursor=total,
                skipped=start - since,
                partial=_as_shown(self._partial),
            )

    def tail(self, count: int) -> list[str]:
        """The last *count* lines, the line still being written included."""
        with self._lock:
            if count <= 0:
                return []
            lines = list(self._lines)[-count:]
            partial = _as_shown(self._partial)
            return (lines + [partial])[-count:] if partial else lines

    def wait(self, timeout: float | None = None) -> bool:
        """Block until it has exited, or *timeout* passes; whether it exited."""
        return self._exited.wait(timeout)

    # -- lifecycle (driven by the manager) -----------------------------------

    def launch(self) -> None:
        """Start the command in its own process group. Raises ``OSError``."""
        self._popen = subprocess.Popen(  # noqa: S602 - a shell is what was approved
            self.command,
            shell=True,
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Its own session, and so its own process group: a stop reaches
            # everything the command started, and a ctrl-c meant for AutoSE
            # does not reach the command.
            start_new_session=True,
        )

    def watch(self, on_exit, on_end=None) -> None:
        """Start reading its output and waiting for it, on two threads.

        *on_exit* is called when the command exits; *on_end* later, when what
        it left running in its group has gone too — only if there was any.
        """
        self._on_end = on_end
        threading.Thread(
            target=self._read, name=f"autose-{self.id}-output", daemon=True
        ).start()
        threading.Thread(
            target=self._wait, args=(on_exit,), name=f"autose-{self.id}-wait", daemon=True
        ).start()

    def signal_stop(self) -> bool:
        """Ask the whole group to end (SIGTERM). False when nothing is left."""
        with self._lock:
            if self._status == RUNNING:
                self._stop_requested = True
            elif self._drained.is_set():
                return False
        return self._signal(signal.SIGTERM)

    def finish_stop(self, deadline: float) -> None:
        """Wait for the group to go until *deadline*, then SIGKILL what is left."""
        self._exited.wait(max(0.0, deadline - time.monotonic()))
        self._drained.wait(max(0.0, deadline - time.monotonic()))
        if self._exited.is_set() and self._drained.is_set():
            return
        self._signal(signal.SIGKILL)
        self._exited.wait(1.0)

    def _signal(self, signum: int) -> bool:
        pid = self.pid
        if pid is None:
            return False
        # The group's id is the leader's pid (it was started as a new
        # session). It is signalled only while the group may still have a
        # member — the leader running, or something still holding the pipe —
        # so a reused id is never hit.
        try:
            os.killpg(pid, signum)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _read(self) -> None:
        popen = self._popen
        assert popen is not None and popen.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # Popen's pipe is a BufferedReader, whose `read1` returns what has
        # arrived instead of waiting for a whole chunk.
        stream = cast(io.BufferedReader, popen.stdout)
        try:
            while True:
                chunk = stream.read1(_READ_CHUNK)
                if not chunk:
                    break
                self._feed(decoder.decode(chunk))
            self._feed(decoder.decode(b"", final=True))
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                last = _as_shown(self._partial)
                if last:
                    self._append(last)
                self._partial = ""
                self._drained.set()
            self._tell_end_if_owed()

    def _feed(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            pending = (self._partial + text).replace("\r\n", "\n")
            *complete, rest = pending.split("\n")
            for line in complete:
                self._append(_as_shown(line))
            self._partial = _unfinished(rest)

    def _append(self, line: str) -> None:
        if len(line) > _MAX_LINE_CHARS:
            line = line[:_MAX_LINE_CHARS] + " …"
        self._lines.append(line)
        self._total += 1

    def _wait(self, on_exit) -> None:
        popen = self._popen
        assert popen is not None
        code = popen.wait()
        self._drained.wait(_DRAIN_SECONDS)
        with self._lock:
            self.exit_code = code
            self.ended_at = time.time()
            self._status = STOPPED if self._stop_requested else EXITED
            self._left_running = self._alive()
        try:
            on_exit(self)
        finally:
            self._exited.set()
            self._tell_end_if_owed()

    def _tell_end_if_owed(self) -> None:
        """Tell, once, that what the exited command left running has gone.

        Without it a list fetched when the command exited would go on showing
        it as still running — and counting it — until something else changed.
        """
        with self._lock:
            owed = (
                self._left_running
                and not self._end_told
                and self._exited.is_set()
                and self._drained.is_set()
            )
            if owed:
                self._end_told = True
        if owed and self._on_end is not None:
            self._on_end(self)


def _unfinished(raw: str) -> str:
    """The text after the last newline, kept raw but bounded.

    Raw, because a carriage return at its end means the next chunk draws over
    it; bounded, because a progress bar that never prints a newline would
    otherwise grow without end. Everything before its last redraw is dropped.
    """
    if "\r" in raw:
        _, redrawn, last = raw.rstrip("\r").rpartition("\r")
        if redrawn:
            raw = last + ("\r" if raw.endswith("\r") else "")
    return raw[-_MAX_LINE_CHARS:]


def _as_shown(line: str) -> str:
    """A line as a terminal would leave it: a carriage return redraws it."""
    if "\r" not in line:
        return line
    parts = [part for part in line.split("\r") if part]
    return parts[-1] if parts else ""


class ProcessManager:
    """The background commands of one session."""

    def __init__(
        self,
        *,
        events: EventSink | None = None,
        max_lines: int = MAX_OUTPUT_LINES,
        stop_grace: float = STOP_GRACE_SECONDS,
        max_running: int = MAX_RUNNING_PER_SESSION,
    ) -> None:
        self._events = events or NullEventSink()
        self._max_lines = max_lines
        self._stop_grace = stop_grace
        self._max_running = max_running
        self._processes: dict[str, BackgroundProcess] = {}
        self._lock = threading.Lock()
        #: Set by the first stop of everything: a run still going when its
        #: session closes cannot start what nothing would stop any more.
        self._closed = False
        _MANAGERS.add(self)

    # -- reading -------------------------------------------------------------

    def processes(self) -> tuple[BackgroundProcess, ...]:
        """Every process still listed, oldest first."""
        with self._lock:
            return tuple(self._processes.values())

    def running(self) -> tuple[BackgroundProcess, ...]:
        return tuple(process for process in self.processes() if process.running)

    def get(self, process_id: str) -> BackgroundProcess:
        """The process with *process_id*, or :class:`UnknownProcessError`."""
        with self._lock:
            process = self._processes.get(str(process_id).strip())
        if process is None:
            raise UnknownProcessError(f"there is no background process '{process_id}' here")
        return process

    # -- changing ------------------------------------------------------------

    def start(self, command: str, *, cwd: Path, shown_cwd: str = ".") -> BackgroundProcess:
        """Start *command* in the background. ``OSError`` if it cannot start."""
        if not SUPPORTED:
            raise ProcessError(
                "this system cannot keep a command running in the background "
                "and stop it again; run it without the trailing '&' so it "
                "finishes on its own"
            )
        process = BackgroundProcess(
            command, cwd=cwd, shown_cwd=shown_cwd, max_lines=self._max_lines
        )
        # Checked, started and listed under one lock, so a close that comes
        # at the same moment either finds it on the list or refuses it.
        with self._lock:
            if self._closed:
                raise ProcessError(
                    "this session has been closed, so nothing more can be started "
                    "in the background"
                )
            # Counted as `alive`: a command that exits but leaves its server
            # running must not be a way past the limit.
            if sum(1 for one in self._processes.values() if one.alive) >= self._max_running:
                raise ProcessError(
                    f"{self._max_running} background commands are already running in "
                    "this session; stop one before starting another"
                )
            process.launch()
            self._processes[process.id] = process
        # Announced before its threads start, so an instant exit can never be
        # told before the start it ends.
        self._emit(
            "process_started",
            command,
            id=process.id,
            command=command,
            cwd=process.shown_cwd,
            pid=process.pid,
            started_at=process.started_at,
        )
        process.watch(self._on_exit, self._on_end)
        return process

    def stop(self, process_id: str, *, wait: bool = True) -> BackgroundProcess:
        """Stop one process and everything it started.

        With *wait*, returns once it has gone (SIGKILL after the grace);
        without, returns at once and the SIGKILL follows on its own thread.
        """
        process = self.get(process_id)
        _stop_together([process], self._stop_grace, wait=wait)
        return process

    def stop_all(self, *, wait: bool = True) -> None:
        """Stop every process this manager started, and start no more — what
        closing a session does."""
        _stop_together(self._close(), self._stop_grace, wait=wait)

    def _close(self) -> tuple[BackgroundProcess, ...]:
        """Refuse every later start, and return what there is to stop."""
        with self._lock:
            self._closed = True
            return tuple(self._processes.values())

    def clear(self, process_id: str) -> None:
        """Take a finished process off the list. A running one must be stopped first."""
        process = self.get(process_id)
        if process.running:
            raise ProcessError(f"{process.id} is still running; stop it before clearing it")
        if process.alive:
            # Off the list, nothing — not even closing the session — would
            # reach what it left running.
            raise ProcessError(
                f"what {process.id} started is still running; stop it before clearing it"
            )
        with self._lock:
            self._processes.pop(process.id, None)
        self._emit("process_cleared", process.command, id=process.id)

    # -- internals -------------------------------------------------------------

    def _on_exit(self, process: BackgroundProcess) -> None:
        status = process.status
        message = (
            f"{process.command} was stopped"
            if status == STOPPED
            else f"{process.command} exited with code {process.exit_code}"
        )
        self._emit(
            "process_exited",
            message,
            id=process.id,
            command=process.command,
            status=status,
            exit_code=process.exit_code,
            ended_at=process.ended_at,
            lines=process.line_count,
            alive=process.alive,
        )

    def _on_end(self, process: BackgroundProcess) -> None:
        self._emit(
            "process_ended",
            f"what {process.command} left running has ended",
            id=process.id,
            command=process.command,
            lines=process.line_count,
        )

    def _emit(self, event_type: str, message: str = "", **data: Any) -> None:
        self._events.emit(Event(type=event_type, message=message, data=data))


def _stop_together(
    processes: Iterable[BackgroundProcess], grace: float, *, wait: bool
) -> None:
    """SIGTERM every group at once, then one shared grace before SIGKILL.

    Shared so stopping six sessions' servers on the way out takes one grace
    period, not six.
    """
    signalled = [process for process in processes if process.signal_stop()]
    if not signalled:
        return
    deadline = time.monotonic() + grace

    def finish() -> None:
        for process in signalled:
            process.finish_stop(deadline)

    if wait:
        finish()
    else:
        threading.Thread(target=finish, name="autose-stop", daemon=True).start()


#: Every live manager, so an exit that skips the orderly close still stops
#: what was started. Weak, so a manager nobody holds is not kept alive by this.
_MANAGERS: weakref.WeakSet[ProcessManager] = weakref.WeakSet()


def stop_managers(managers: Iterable[ProcessManager], *, wait: bool = True) -> None:
    """Stop what each of *managers* started, under one shared grace period.

    What an Adapter closing several sessions at once calls — the dashboard
    shutting down — so six sessions' servers take one grace, not six.
    """
    # The managers' own module: closing them together is why _close exists.
    processes = [
        process for manager in managers for process in manager._close()  # noqa: SLF001
    ]
    _stop_together(processes, STOP_GRACE_SECONDS, wait=wait)


def stop_every_process() -> None:
    """Stop what every live manager started. Registered with ``atexit``."""
    stop_managers(list(_MANAGERS))


atexit.register(stop_every_process)


@contextlib.contextmanager
def exit_cleanly_on_termination() -> Iterator[None]:
    """While inside, SIGTERM and SIGHUP end AutoSE as an ordinary exit.

    Each background command runs in its own session, so neither signal
    reaches it: a closed terminal window (SIGHUP) or a plain ``kill``
    (SIGTERM) would end AutoSE at once and leave every server it started
    running with nobody to stop it. Raising ``SystemExit`` instead lets the
    Adapter's ``finally`` close its sessions, and ``atexit`` catch the rest.

    Wrapped around an Adapter's main loop, in the main thread; the previous
    handlers come back on the way out. A signal that already has a handler
    of its own is left alone.
    """
    installed: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if signum is not None and signal.getsignal(signum) is signal.SIG_DFL:
                installed[signum] = signal.signal(signum, _exit_on_signal)
    try:
        yield
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)


def _exit_on_signal(signum, frame) -> None:
    raise SystemExit(128 + signum)
