"""Work that runs while nobody is watching: schedules and watchers (DR-047).

Two kinds of standing instruction, kept per workspace in
``.autose/automation.json``:

* a **schedule** starts a prompt on a cadence — every *n* minutes, or on a
  five-field cron expression read in local time;
* a **watcher** runs a shell command the user wrote, on an interval, and starts
  a prompt when the result is news: the output *changed*, or the command
  *failed*.

Everything here is pure in the sense that matters for testing: there are no
threads and no clock. Every question takes ``now`` (epoch seconds) from the
caller, and running a watcher's command goes through an injected
:class:`CommandRunner`. *When* to ask — a ticking loop — and *what* to do with
an answer — opening a session — belong to an adapter; the dashboard's runner
lives in ``gui/``.

Three seams are Strategy interfaces rather than branches (DR-002): a
:class:`Cadence` says when a schedule is next due, a :class:`WatchTrigger` says
whether a watcher's check is worth a run, and a :class:`CommandTrust` says
whether a watcher's command may run at all.

The last of those exists because the definitions live in the workspace. The
user writes a watcher's command in the dashboard, and that authorship is what
lets it run without the per-call approval gate (DR-012) — the same trust as a
command in a config file the user wrote. A command that merely *arrived* in
``.autose/automation.json`` — in a clone, a pull, a teammate's commit — was
authored by nobody on this machine, so a command only runs once it has been
saved through :class:`AutomationStore` on this machine, which records its
digest outside the workspace.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .common.tools.shell import refuse_escaping_command
from .errors import AutoSEError
from .tiers import is_tier
from .user_config import atomic_write_text

__all__ = [
    "AUTOMATION_FILE",
    "OUTPUT_LIMIT",
    "WATCH_TIMEOUT_SECONDS",
    "AutomationError",
    "InvalidAutomationError",
    "UnknownAutomationError",
    "Cadence",
    "IntervalCadence",
    "CronCadence",
    "parse_cadence",
    "WatchTrigger",
    "Observation",
    "TRIGGERS",
    "CommandResult",
    "CommandRunner",
    "run_watch_command",
    "CommandTrust",
    "RememberedTrust",
    "TrustFile",
    "Schedule",
    "Watcher",
    "Due",
    "WatchOutcome",
    "AutomationStore",
    "render_prompt",
    "bound_output",
]

#: Workspace-relative home of the definitions. Under ``.autose`` with
#: everything else AutoSE keeps per workspace.
AUTOMATION_FILE = Path(".autose") / "automation.json"

#: Characters of a watcher's output put into the prompt it starts. The tail is
#: kept: a build log's verdict is at its end.
OUTPUT_LIMIT = 4_000

#: How long one watcher check may run. A watcher is a quick probe — a status
#: query, a test of one thing — not a build; a hung one must not stall every
#: other schedule behind it.
WATCH_TIMEOUT_SECONDS = 60.0

#: How far ahead a cron expression is searched for its next match. Eight years
#: covers the longest gap a valid expression can have (29 February across a
#: skipped leap year, as in 2100).
_SEARCH_DAYS = 366 * 8

_NAME_LIMIT = 80
_PROMPT_LIMIT = 20_000
_COMMAND_LIMIT = 2_000

_FORMAT_VERSION = 1


# -- errors --------------------------------------------------------------------


class AutomationError(AutoSEError):
    """A schedule or watcher request that cannot be carried out."""


class InvalidAutomationError(AutomationError):
    """A definition that does not validate. The message says what to fix."""


class UnknownAutomationError(AutomationError):
    """No schedule or watcher has that id."""


# -- cadence -------------------------------------------------------------------


class Cadence(Protocol):
    """When a schedule is next due, after a given moment."""

    @property
    def spec(self) -> int | str:
        """The value as the user wrote it: minutes, or a cron expression."""

    def next_after(self, moment: float) -> float:
        """The first due time strictly after *moment* (epoch seconds)."""

    def describe(self) -> str:
        """One line a person reads, e.g. ``every 15 minutes``."""


@dataclass(frozen=True)
class IntervalCadence:
    """Every *minutes* minutes, counted from the last run."""

    minutes: int

    @property
    def spec(self) -> int:
        return self.minutes

    def next_after(self, moment: float) -> float:
        return moment + self.minutes * 60.0

    def describe(self) -> str:
        return "every minute" if self.minutes == 1 else f"every {self.minutes} minutes"


_CRON_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


@dataclass(frozen=True)
class CronCadence:
    """A five-field cron expression, matched against local time.

    Fields are minute, hour, day of month, month and day of week (0 or 7 is
    Sunday). Each field takes a number, ``*``, a range ``a-b``, a step ``*/n``
    or ``a-b/n``, or a comma list of those. As in cron itself, when both the
    day of month and the day of week are restricted a day matching *either*
    is due.
    """

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> CronCadence:
        text = " ".join(str(expression).split())
        parts = text.split(" ")
        if len(parts) != 5:
            raise InvalidAutomationError(
                f"'{text}' is not a cron expression: it needs five fields "
                "(minute hour day-of-month month day-of-week), e.g. '0 9 * * 1-5'"
            )
        values = [
            _parse_cron_field(part, name, low, high)
            for part, (name, low, high) in zip(parts, _CRON_FIELDS, strict=True)
        ]
        weekdays = frozenset(0 if day == 7 else day for day in values[4])
        cadence = cls(
            expression=text,
            minutes=values[0],
            hours=values[1],
            days=values[2],
            months=values[3],
            weekdays=weekdays,
            days_restricted=not parts[2].startswith("*"),
            weekdays_restricted=not parts[4].startswith("*"),
        )
        # An expression can be well formed and still never fire — the 31st
        # of February. Saying so now beats a schedule that silently never runs.
        reference = datetime(2024, 1, 1).timestamp()
        if cadence._search(reference) is None:
            raise InvalidAutomationError(f"'{text}' never matches a real date")
        return cadence

    @property
    def spec(self) -> str:
        return self.expression

    def describe(self) -> str:
        return f"cron {self.expression}"

    def next_after(self, moment: float) -> float:
        found = self._search(moment)
        if found is None:  # pragma: no cover - parse() refuses such expressions
            raise InvalidAutomationError(f"'{self.expression}' never matches a real date")
        return found

    def _search(self, moment: float) -> float | None:
        start = datetime.fromtimestamp(moment).replace(second=0, microsecond=0)
        start += timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        for _ in range(_SEARCH_DAYS):
            if day.month in self.months and self._day_matches(day):
                for hour in hours:
                    for minute in minutes:
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate < start:
                            continue
                        stamp = candidate.timestamp()
                        # A local time skipped by a daylight-saving change can
                        # map to an instant before *moment*; never go back.
                        if stamp > moment:
                            return stamp
            day += timedelta(days=1)
        return None

    def _day_matches(self, day: datetime) -> bool:
        in_month = day.day in self.days
        # Python counts Monday as 0; cron counts Sunday as 0.
        in_week = (day.weekday() + 1) % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return in_month or in_week
        return in_month and in_week


def _parse_cron_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise InvalidAutomationError(f"the {name} field '{text}' has an empty item")
        base, _, step_text = item.partition("/")
        step = 1
        if step_text or item.endswith("/"):
            step = _cron_number(step_text, name, item)
            if step < 1:
                raise InvalidAutomationError(
                    f"the {name} field '{item}' has a step of 0; use 1 or more"
                )
        if base == "*":
            first, last = low, high
        elif "-" in base:
            left, _, right = base.partition("-")
            first, last = _cron_number(left, name, item), _cron_number(right, name, item)
            if first > last:
                raise InvalidAutomationError(
                    f"the {name} range '{base}' runs backwards; write it low-high"
                )
        else:
            first = _cron_number(base, name, item)
            # `5/15` means "from 5, every 15", as in cron.
            last = high if step_text else first
        for value in (first, last):
            if not low <= value <= high:
                raise InvalidAutomationError(
                    f"the {name} field allows {low}-{high}; '{item}' is outside it"
                )
        values.update(range(first, last + 1, step))
    return frozenset(values)


def _cron_number(text: str, name: str, item: str) -> int:
    if not text.isdigit() or not text.isascii():
        raise InvalidAutomationError(
            f"the {name} field '{item}' is not understood: use numbers, '*', "
            "a-b ranges, */n steps or comma lists"
        )
    return int(text)


def parse_cadence(every: Any) -> Cadence:
    """A schedule's ``every``: whole minutes (1 or more), or a cron expression."""
    if isinstance(every, bool):
        raise InvalidAutomationError(_EVERY_HINT)
    if isinstance(every, int):
        return _interval(every)
    if isinstance(every, float) and every.is_integer():
        return _interval(int(every))
    if isinstance(every, str):
        text = every.strip()
        if text.isdigit() and text.isascii():
            return _interval(int(text))
        if len(text.split()) == 5:
            return CronCadence.parse(text)
    raise InvalidAutomationError(_EVERY_HINT)


_EVERY_HINT = (
    "every must be a number of minutes (1 or more) or a five-field cron "
    "expression such as '0 9 * * 1-5'"
)


def _interval(minutes: int) -> IntervalCadence:
    if minutes < 1:
        raise InvalidAutomationError("an interval must be at least 1 minute")
    return IntervalCadence(minutes)


# -- watcher triggers ------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What one watcher check saw: how the command ended and what it printed."""

    exit_code: int
    digest: str


class WatchTrigger(Protocol):
    """Whether a check is news worth starting a run for."""

    def fires(self, previous: Observation, current: Observation) -> bool:
        """Compare against the previous check. Never asked on the first one."""


class OnChange:
    """Fires whenever the output differs from the previous check's."""

    def fires(self, previous: Observation, current: Observation) -> bool:
        return previous.digest != current.digest


class OnFailure:
    """Fires when the command fails *anew*.

    A non-zero exit after a success, or a failure whose output changed. A
    failure that persists unchanged does not start a run every interval — the
    first one already did.
    """

    def fires(self, previous: Observation, current: Observation) -> bool:
        if current.exit_code == 0:
            return False
        return previous.exit_code == 0 or previous.digest != current.digest


#: The triggers a watcher can name. A new kind is one class and one entry here.
TRIGGERS: dict[str, WatchTrigger] = {"change": OnChange(), "failure": OnFailure()}


# -- running a watcher's command ----------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str


class CommandRunner(Protocol):
    def __call__(self, command: str, *, root: Path, timeout: float) -> CommandResult:
        """Run *command* in *root* and report how it ended."""


def run_watch_command(
    command: str, *, root: Path, timeout: float = WATCH_TIMEOUT_SECONDS
) -> CommandResult:
    """Run a watcher's command in the workspace, under ``run_command``'s checks.

    The same workspace confinement as the agent's shell tool, and a timeout
    that cannot be turned off. A command that times out or cannot start is a
    failure (exit code -1), so a ``failure`` watcher notices a hung probe.
    """
    refusal = refuse_escaping_command(command)
    if refusal:
        raise InvalidAutomationError(refusal.removeprefix("Error: "))
    try:
        completed = subprocess.run(  # noqa: S602 - the user's own command, by design
            command,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(-1, f"the command timed out after {timeout:g} seconds")
    except OSError as exc:
        return CommandResult(-1, f"the command could not run: {exc}")
    output = completed.stdout or ""
    if completed.stderr:
        output += ("\n" if output else "") + completed.stderr
    return CommandResult(completed.returncode, output)


def bound_output(output: str, limit: int = OUTPUT_LIMIT) -> str:
    """The tail of *output*, at most *limit* characters, saying what was cut."""
    text = output.strip()
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return f"[… {cut} earlier characters cut]\n{text[-limit:]}"


def render_prompt(template: str, *, output: str, exit_code: int) -> str:
    """A watcher's prompt with ``{output}`` and ``{exit_code}`` filled in.

    Plain replacement rather than ``str.format``: command output is full of
    braces, and a template a person wrote should not need escaping.
    """
    return template.replace("{exit_code}", str(exit_code)).replace(
        "{output}", bound_output(output)
    )


def _digest(output: str) -> str:
    return hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()


# -- trust in a watcher's command ------------------------------------------------------


class CommandTrust(Protocol):
    """Which watcher commands this machine's user has authored."""

    def allows(self, command: str) -> bool: ...

    def trust(self, command: str) -> None: ...


class RememberedTrust:
    """Trust for this process only: whatever was saved since it started."""

    def __init__(self) -> None:
        self._digests: set[str] = set()

    def allows(self, command: str) -> bool:
        return _digest(command) in self._digests

    def trust(self, command: str) -> None:
        self._digests.add(_digest(command))


class TrustFile:
    """Trust kept in the user's config directory, per workspace.

    Outside the workspace on purpose: a file in the repository cannot vouch
    for a command in that same repository.
    """

    def __init__(self, path: Path, workspace: Path) -> None:
        self._path = path
        self._key = str(workspace.resolve())
        self._lock = threading.Lock()

    def allows(self, command: str) -> bool:
        with self._lock:
            return _digest(command) in self._read().get(self._key, [])

    def trust(self, command: str) -> None:
        with self._lock:
            document = self._read()
            known = list(document.get(self._key, []))
            digest = _digest(command)
            if digest in known:
                return
            known.append(digest)
            document[self._key] = known[-200:]
            atomic_write_text(self._path, json.dumps(document, indent=1))

    def _read(self) -> dict[str, list[str]]:
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            str(key): [str(item) for item in value]
            for key, value in loaded.items()
            if isinstance(value, list)
        }


# -- the definitions ------------------------------------------------------------------


def _new_id() -> str:
    return secrets.token_hex(6)


@dataclass
class Schedule:
    """A prompt started on a cadence."""

    id: str
    name: str
    prompt: str
    every: int | str
    tier: str = ""
    enabled: bool = True
    created_at: float = 0.0
    last_run_at: float | None = None
    last_session: str = ""
    next_run_at: float | None = None
    #: What happened the last time it was due: ``started``, or why not.
    last_outcome: str = ""

    @property
    def cadence(self) -> Cadence:
        return parse_cadence(self.every)

    def refresh(self) -> None:
        """Recompute :attr:`next_run_at` from the last run (or creation)."""
        if not self.enabled:
            self.next_run_at = None
            return
        anchor = self.last_run_at if self.last_run_at is not None else self.created_at
        self.next_run_at = self.cadence.next_after(anchor)

    def to_dict(self) -> dict[str, Any]:
        cadence = self.cadence
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "tier": self.tier,
            "every": self.every,
            "every_kind": "interval" if isinstance(cadence, IntervalCadence) else "cron",
            "every_text": cadence.describe(),
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_session": self.last_session,
            "next_run_at": self.next_run_at,
            "last_outcome": self.last_outcome,
        }


@dataclass
class Watcher:
    """A user's command, checked on an interval, that starts a prompt on news."""

    id: str
    name: str
    command: str
    every_minutes: int
    trigger: str
    prompt_template: str
    tier: str = ""
    enabled: bool = True
    created_at: float = 0.0
    last_checked_at: float | None = None
    last_output_digest: str = ""
    last_exit_code: int | None = None
    last_triggered_at: float | None = None
    last_session: str = ""
    last_outcome: str = ""
    #: A triggered prompt that could not start yet (the desk was full). It is
    #: retried on the next tick rather than lost, without re-running the check.
    pending_prompt: str = ""

    @property
    def next_check_at(self) -> float | None:
        if not self.enabled:
            return None
        if self.last_checked_at is None:
            return self.created_at
        return self.last_checked_at + self.every_minutes * 60.0

    @property
    def has_baseline(self) -> bool:
        return bool(self.last_output_digest) or self.last_exit_code is not None

    def to_dict(self, *, trusted: bool) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "every_minutes": self.every_minutes,
            "trigger": self.trigger,
            "prompt_template": self.prompt_template,
            "tier": self.tier,
            "enabled": self.enabled,
            "trusted": trusted,
            "created_at": self.created_at,
            "last_checked_at": self.last_checked_at,
            "last_output_digest": self.last_output_digest,
            "last_exit_code": self.last_exit_code,
            "last_triggered_at": self.last_triggered_at,
            "last_session": self.last_session,
            "last_outcome": self.last_outcome,
            "next_check_at": self.next_check_at,
            "pending": bool(self.pending_prompt),
        }


@dataclass(frozen=True)
class Due:
    """What a tick should act on at one moment."""

    schedules: tuple[Schedule, ...] = ()
    #: Watchers whose interval has elapsed: run their check.
    checks: tuple[Watcher, ...] = ()
    #: Watchers holding a triggered prompt that has not started yet.
    pending: tuple[Watcher, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.schedules or self.checks or self.pending)


@dataclass(frozen=True)
class WatchOutcome:
    """One watcher check, and whether it should start a run."""

    watcher_id: str
    triggered: bool = False
    baseline: bool = False
    exit_code: int | None = None
    output: str = ""
    prompt: str = ""
    #: Why nothing ran, when the check itself did not happen.
    refused: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.watcher_id,
            "triggered": self.triggered,
            "baseline": self.baseline,
            "exit_code": self.exit_code,
            "output": bound_output(self.output),
            "refused": self.refused,
        }


# -- the store ---------------------------------------------------------------------------


_SCHEDULE_FIELDS = ("name", "prompt", "every", "tier", "enabled")
_WATCHER_FIELDS = (
    "name",
    "command",
    "every_minutes",
    "trigger",
    "prompt_template",
    "tier",
    "enabled",
)


class AutomationStore:
    """A workspace's schedules and watchers, saved atomically on every change.

    Thread-safe — the dashboard's ticking runner and its HTTP handlers share
    one — but it never starts a thread and never reads a clock: ``now`` is
    always the caller's.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        trust: CommandTrust | None = None,
        path: Path | None = None,
    ) -> None:
        self._root = Path(workspace_root)
        self._path = path or self._root / AUTOMATION_FILE
        self._trust: CommandTrust = trust or RememberedTrust()
        self._lock = threading.RLock()
        self._schedules: dict[str, Schedule] = {}
        self._watchers: dict[str, Watcher] = {}
        #: Why the file could not be read, for the dashboard to show. Never
        #: raised: a broken file must not stop the dashboard from opening.
        self.problems: list[str] = []
        self._unreadable = False
        #: The file as last read or written, so a change made outside this
        #: store — a `git pull`, a hand edit — is read before it is acted on
        #: instead of being overwritten by the next save.
        self._seen: tuple[int, int] | None = None
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def workspace_root(self) -> Path:
        return self._root

    # -- reading -----------------------------------------------------------------

    def schedules(self) -> list[Schedule]:
        with self._fresh():
            return [replace(item) for item in self._schedules.values()]

    def watchers(self) -> list[Watcher]:
        with self._fresh():
            return [replace(item) for item in self._watchers.values()]

    def schedule(self, schedule_id: str) -> Schedule:
        with self._fresh():
            return replace(self._schedule(schedule_id))

    def watcher(self, watcher_id: str) -> Watcher:
        with self._fresh():
            return replace(self._watcher(watcher_id))

    def trusted(self, watcher: Watcher) -> bool:
        return self._trust.allows(watcher.command)

    def payload(self) -> dict[str, Any]:
        with self._fresh():
            return {
                "schedules": [item.to_dict() for item in self._schedules.values()],
                "watchers": [
                    item.to_dict(trusted=self.trusted(item))
                    for item in self._watchers.values()
                ],
                "problems": list(self.problems),
            }

    def summary(self) -> dict[str, Any]:
        with self._fresh():
            runs = [
                item.next_run_at
                for item in self._schedules.values()
                if item.enabled and item.next_run_at is not None
            ]
            checks = [
                at
                for item in self._watchers.values()
                if (at := item.next_check_at) is not None
            ]
            return {
                "schedules": len(self._schedules),
                "watchers": len(self._watchers),
                "next_run_at": min(runs) if runs else None,
                "next_check_at": min(checks) if checks else None,
            }

    def due(self, now: float) -> Due:
        """What is due at *now*: schedules to run, watchers to check or retry."""
        with self._fresh():
            schedules = tuple(
                replace(item)
                for item in self._schedules.values()
                if item.enabled and item.next_run_at is not None and item.next_run_at <= now
            )
            pending = tuple(
                replace(item)
                for item in self._watchers.values()
                if item.enabled and item.pending_prompt
            )
            checks = tuple(
                replace(item)
                for item in self._watchers.values()
                if item.enabled
                and not item.pending_prompt
                and (at := item.next_check_at) is not None
                and at <= now
            )
        return Due(schedules=schedules, checks=checks, pending=pending)

    # -- schedules ------------------------------------------------------------------

    def add_schedule(self, fields: Mapping[str, Any], *, now: float) -> Schedule:
        values = _schedule_values(fields, required=True)
        schedule = Schedule(id=_new_id(), created_at=now, **values)
        schedule.refresh()
        with self._fresh():
            self._schedules[schedule.id] = schedule
            self._save()
            return replace(schedule)

    def update_schedule(
        self, schedule_id: str, fields: Mapping[str, Any], *, now: float
    ) -> Schedule:
        values = _schedule_values(fields, required=False)
        with self._fresh():
            schedule = self._schedule(schedule_id)
            cadence_changed = "every" in values and values["every"] != schedule.every
            enabling = values.get("enabled") is True and not schedule.enabled
            for key, value in values.items():
                setattr(schedule, key, value)
            if cadence_changed or enabling:
                # A new cadence, or switching back on, counts from now: a
                # schedule that was off all week must not fire the moment it
                # is enabled because its last run was a week ago.
                schedule.created_at = now
                schedule.last_run_at = None
            schedule.refresh()
            self._save()
            return replace(schedule)

    def delete_schedule(self, schedule_id: str) -> None:
        with self._fresh():
            self._schedule(schedule_id)
            del self._schedules[schedule_id]
            self._save()

    def record_schedule_run(self, schedule_id: str, *, now: float, session: str) -> Schedule:
        """The schedule started *session* at *now*; its next run counts from here.

        Runs missed while nothing was ticking collapse into this one: a laptop
        asleep all night runs an hourly schedule once on waking, not eight
        times.
        """
        with self._fresh():
            schedule = self._schedule(schedule_id)
            schedule.last_run_at = now
            schedule.last_session = session
            schedule.last_outcome = "started"
            schedule.refresh()
            self._save()
            return replace(schedule)

    def record_schedule_skipped(self, schedule_id: str, *, reason: str) -> Schedule:
        """The schedule was due but could not start; it stays due."""
        with self._fresh():
            schedule = self._schedule(schedule_id)
            schedule.last_outcome = f"skipped: {reason}"
            self._save()
            return replace(schedule)

    # -- watchers ---------------------------------------------------------------------

    def add_watcher(self, fields: Mapping[str, Any], *, now: float) -> Watcher:
        values = _watcher_values(fields, required=True)
        watcher = Watcher(id=_new_id(), created_at=now, **values)
        with self._fresh():
            self._trust.trust(watcher.command)
            self._watchers[watcher.id] = watcher
            self._save()
            return replace(watcher)

    def update_watcher(
        self, watcher_id: str, fields: Mapping[str, Any], *, now: float
    ) -> Watcher:
        """Change a watcher. Saving its command here is what trusts it.

        Only a change that carries the command does: switching a watcher on
        or renaming it must not quietly approve a command that arrived in a
        pull and that nobody has looked at.
        """
        values = _watcher_values(fields, required=False)
        with self._fresh():
            watcher = self._watcher(watcher_id)
            if "command" in values and values["command"] != watcher.command:
                # A different command has no history to compare against.
                watcher.last_output_digest = ""
                watcher.last_exit_code = None
                watcher.last_checked_at = None
                watcher.pending_prompt = ""
                watcher.created_at = now
            for key, value in values.items():
                setattr(watcher, key, value)
            if not watcher.enabled:
                watcher.pending_prompt = ""
            if "command" in values:
                self._trust.trust(watcher.command)
            self._save()
            return replace(watcher)

    def delete_watcher(self, watcher_id: str) -> None:
        with self._fresh():
            self._watcher(watcher_id)
            del self._watchers[watcher_id]
            self._save()

    def check_watcher(
        self,
        watcher_id: str,
        *,
        now: float,
        runner: CommandRunner = run_watch_command,
        timeout: float = WATCH_TIMEOUT_SECONDS,
    ) -> WatchOutcome:
        """Run one watcher's command and decide whether it is news.

        The first check only records a baseline. The command runs outside the
        store's lock, so a slow probe never holds up the dashboard.
        """
        with self._fresh():
            watcher = replace(self._watcher(watcher_id))
        if not self._trust.allows(watcher.command):
            reason = (
                "this command was not saved on this machine; open the watcher "
                "in the dashboard and save it to let it run"
            )
            self._after_check(watcher, now=now, outcome=f"refused: {reason}")
            return WatchOutcome(watcher_id, refused=reason)
        try:
            result = runner(watcher.command, root=self._root, timeout=timeout)
        except InvalidAutomationError as exc:
            self._after_check(watcher, now=now, outcome=f"refused: {exc}")
            return WatchOutcome(watcher_id, refused=str(exc))
        current = Observation(result.exit_code, _digest(result.output))
        baseline = not watcher.has_baseline
        trigger = TRIGGERS.get(watcher.trigger, TRIGGERS["change"])
        triggered = not baseline and trigger.fires(
            Observation(
                watcher.last_exit_code if watcher.last_exit_code is not None else 0,
                watcher.last_output_digest,
            ),
            current,
        )
        prompt = (
            render_prompt(
                watcher.prompt_template, output=result.output, exit_code=result.exit_code
            )
            if triggered
            else ""
        )
        outcome = "baseline recorded" if baseline else ("triggered" if triggered else "no change")
        self._after_check(
            watcher, now=now, outcome=outcome, observation=current, pending_prompt=prompt
        )
        return WatchOutcome(
            watcher_id,
            triggered=triggered,
            baseline=baseline,
            exit_code=result.exit_code,
            output=result.output,
            prompt=prompt,
        )

    def record_watcher_started(self, watcher_id: str, *, now: float, session: str) -> Watcher:
        with self._fresh():
            watcher = self._watcher(watcher_id)
            watcher.pending_prompt = ""
            watcher.last_triggered_at = now
            watcher.last_session = session
            watcher.last_outcome = "started"
            self._save()
            return replace(watcher)

    def record_watcher_skipped(self, watcher_id: str, *, reason: str) -> Watcher:
        """A triggered prompt could not start; it is kept to retry next tick."""
        with self._fresh():
            watcher = self._watcher(watcher_id)
            watcher.last_outcome = f"skipped: {reason}"
            self._save()
            return replace(watcher)

    def _after_check(
        self,
        snapshot: Watcher,
        *,
        now: float,
        outcome: str,
        observation: Observation | None = None,
        pending_prompt: str = "",
    ) -> None:
        with self._fresh():
            watcher = self._watchers.get(snapshot.id)
            if watcher is None or watcher.command != snapshot.command:
                # Deleted or rewritten while its old command ran: the result
                # describes something that no longer exists.
                return
            watcher.last_checked_at = now
            watcher.last_outcome = outcome
            if observation is not None:
                watcher.last_exit_code = observation.exit_code
                watcher.last_output_digest = observation.digest
            if pending_prompt:
                watcher.pending_prompt = pending_prompt
            self._save()

    # -- persistence ------------------------------------------------------------------

    def _schedule(self, schedule_id: str) -> Schedule:
        found = self._schedules.get(schedule_id)
        if found is None:
            raise UnknownAutomationError(f"no schedule has the id '{schedule_id}'")
        return found

    def _watcher(self, watcher_id: str) -> Watcher:
        found = self._watchers.get(watcher_id)
        if found is None:
            raise UnknownAutomationError(f"no watcher has the id '{watcher_id}'")
        return found

    @contextlib.contextmanager
    def _fresh(self) -> Iterator[None]:
        """Hold the lock, having re-read the file if it changed underneath."""
        with self._lock:
            if self._stamp() != self._seen:
                self._schedules.clear()
                self._watchers.clear()
                self.problems.clear()
                self._unreadable = False
                self._load()
            yield

    def _stamp(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> None:
        self._seen = self._stamp()
        if not self._path.exists():
            return
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._mark_unreadable(f"{self._path} could not be read: {exc}")
            return
        if not isinstance(document, dict):
            self._mark_unreadable(f"{self._path} does not hold a JSON object")
            return
        for raw in _list(document.get("schedules")):
            try:
                schedule = _schedule_from(raw)
            except (InvalidAutomationError, KeyError, TypeError, ValueError) as exc:
                self._mark_unreadable(f"a schedule in {self._path} was skipped: {exc}")
                continue
            self._schedules[schedule.id] = schedule
        for raw in _list(document.get("watchers")):
            try:
                watcher = _watcher_from(raw)
            except (InvalidAutomationError, KeyError, TypeError, ValueError) as exc:
                self._mark_unreadable(f"a watcher in {self._path} was skipped: {exc}")
                continue
            self._watchers[watcher.id] = watcher

    def _mark_unreadable(self, problem: str) -> None:
        self._unreadable = True
        self.problems.append(problem)
        del self.problems[:-10]

    def _save(self) -> None:
        """Write everything, atomically. Caller holds the lock."""
        if self._unreadable and self._path.exists():
            # Never overwrite what could not be read: it may be the only copy
            # of someone's definitions. Keep it beside the new file.
            kept = self._path.with_name(self._path.name + ".unreadable")
            with contextlib.suppress(OSError):
                os.replace(self._path, kept)
                self.problems.append(f"the unreadable file was kept as {kept}")
        self._unreadable = False
        document = {
            "version": _FORMAT_VERSION,
            "schedules": [_schedule_record(item) for item in self._schedules.values()],
            "watchers": [_watcher_record(item) for item in self._watchers.values()],
        }
        atomic_write_text(
            self._path, json.dumps(document, ensure_ascii=False, indent=1), mode=0o600
        )
        self._seen = self._stamp()


# -- validation and (de)serialisation ------------------------------------------------------


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(fields: Mapping[str, Any], key: str, limit: int, label: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidAutomationError(f"{label} is required")
    text = value.strip()
    if len(text) > limit:
        raise InvalidAutomationError(f"{label} is longer than {limit} characters")
    return text


def _tier(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("", "auto"):
        return ""
    if not is_tier(text):
        raise InvalidAutomationError(f"'{text}' is not a tier; leave it empty for auto")
    return text


def _enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidAutomationError("enabled must be true or false")
    return value


def _minutes(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidAutomationError("every_minutes must be a whole number of minutes")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise InvalidAutomationError("every_minutes must be a whole number of minutes")
    if value < 1:
        raise InvalidAutomationError("every_minutes must be at least 1")
    return value


def _command(value: Any) -> str:
    text = _text({"command": value}, "command", _COMMAND_LIMIT, "command")
    refusal = refuse_escaping_command(text)
    if refusal:
        raise InvalidAutomationError(refusal.removeprefix("Error: "))
    return text


def _collect(
    fields: Mapping[str, Any],
    allowed: tuple[str, ...],
    parsers: Mapping[str, Callable[[Any], Any]],
    *,
    required: bool,
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in allowed:
        if key in fields:
            values[key] = parsers[key](fields[key])
        elif required:
            if key in defaults:
                values[key] = defaults[key]
            else:
                values[key] = parsers[key](None)  # raises "… is required"
    return values


def _schedule_values(fields: Mapping[str, Any], *, required: bool) -> dict[str, Any]:
    parsers: dict[str, Callable[[Any], Any]] = {
        "name": lambda v: _text({"v": v}, "v", _NAME_LIMIT, "a name"),
        "prompt": lambda v: _text({"v": v}, "v", _PROMPT_LIMIT, "a prompt"),
        "every": lambda v: parse_cadence(v).spec,
        "tier": _tier,
        "enabled": _enabled,
    }
    return _collect(
        fields,
        _SCHEDULE_FIELDS,
        parsers,
        required=required,
        defaults={"tier": "", "enabled": True},
    )


def _trigger(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in TRIGGERS:
        raise InvalidAutomationError(
            f"trigger must be one of {', '.join(sorted(TRIGGERS))}"
        )
    return text


def _watcher_values(fields: Mapping[str, Any], *, required: bool) -> dict[str, Any]:
    parsers: dict[str, Callable[[Any], Any]] = {
        "name": lambda v: _text({"v": v}, "v", _NAME_LIMIT, "a name"),
        "command": _command,
        "every_minutes": _minutes,
        "trigger": _trigger,
        "prompt_template": lambda v: _text({"v": v}, "v", _PROMPT_LIMIT, "a prompt template"),
        "tier": _tier,
        "enabled": _enabled,
    }
    return _collect(
        fields,
        _WATCHER_FIELDS,
        parsers,
        required=required,
        defaults={"tier": "", "enabled": True, "trigger": "change"},
    )


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _schedule_record(item: Schedule) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "prompt": item.prompt,
        "every": item.every,
        "tier": item.tier,
        "enabled": item.enabled,
        "created_at": item.created_at,
        "last_run_at": item.last_run_at,
        "last_session": item.last_session,
        "next_run_at": item.next_run_at,
        "last_outcome": item.last_outcome,
    }


def _schedule_from(raw: Any) -> Schedule:
    if not isinstance(raw, dict):
        raise TypeError("not an object")
    values = _schedule_values(raw, required=True)
    schedule = Schedule(
        id=str(raw["id"]),
        created_at=_optional_float(raw.get("created_at")) or 0.0,
        last_run_at=_optional_float(raw.get("last_run_at")),
        last_session=str(raw.get("last_session") or ""),
        last_outcome=str(raw.get("last_outcome") or ""),
        **values,
    )
    # Recomputed rather than trusted, so a hand-edited cadence takes effect.
    schedule.refresh()
    return schedule


def _watcher_record(item: Watcher) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "command": item.command,
        "every_minutes": item.every_minutes,
        "trigger": item.trigger,
        "prompt_template": item.prompt_template,
        "tier": item.tier,
        "enabled": item.enabled,
        "created_at": item.created_at,
        "last_checked_at": item.last_checked_at,
        "last_output_digest": item.last_output_digest,
        "last_exit_code": item.last_exit_code,
        "last_triggered_at": item.last_triggered_at,
        "last_session": item.last_session,
        "last_outcome": item.last_outcome,
        "pending_prompt": item.pending_prompt,
    }


def _watcher_from(raw: Any) -> Watcher:
    if not isinstance(raw, dict):
        raise TypeError("not an object")
    values = _watcher_values(raw, required=True)
    exit_code = raw.get("last_exit_code")
    return Watcher(
        id=str(raw["id"]),
        created_at=_optional_float(raw.get("created_at")) or 0.0,
        last_checked_at=_optional_float(raw.get("last_checked_at")),
        last_output_digest=str(raw.get("last_output_digest") or ""),
        last_exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        last_triggered_at=_optional_float(raw.get("last_triggered_at")),
        last_session=str(raw.get("last_session") or ""),
        last_outcome=str(raw.get("last_outcome") or ""),
        pending_prompt=str(raw.get("pending_prompt") or ""),
        **values,
    )

