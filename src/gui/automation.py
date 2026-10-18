"""The ticking half of automation: the dashboard as a long-running gateway (DR-047).

:mod:`logic.automation` knows what is due and whether a watcher's check is
news; it never reads a clock and never starts a thread. This module is the
adapter that does both: a daemon thread wakes every :data:`TICK_SECONDS`, asks
the store what is due at that moment, and for each due schedule or triggered
watcher asks the desk to open a new session and run the prompt there.

The desk is the :class:`~gui.bridge.SessionBridge`, reached through the small
:class:`Desk` protocol so this module never imports it. What it must provide:
open an *unattended* session (one whose approvals are refused at once unless
the dashboard was started with ``--yes`` — nobody is there to answer them),
say whether a session is still working, and publish a frame.

A run that cannot start — the desk is full, there is no model — is recorded as
skipped and stays due, so it is retried on the next tick.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from logic.automation import (
    AutomationStore,
    CommandRunner,
    Schedule,
    UnknownAutomationError,
    Watcher,
    WatchOutcome,
    run_watch_command,
)

__all__ = ["TICK_SECONDS", "Desk", "AutomationRunner", "SkippedRun"]

#: How often the runner looks for due work. Cron's resolution is a minute, so
#: half of one keeps a schedule within a minute of its time without spinning.
TICK_SECONDS = 30.0


class SkippedRun(Exception):
    """A due run that could not start now, and why. It stays due."""


class Desk(Protocol):
    def start_unattended(
        self, *, title: str, prompt: str, tier: str | None, origin: dict[str, Any]
    ) -> str:
        """Open a session nobody is watching, start *prompt*, return its id.

        Raises :class:`SkippedRun` (or another exception, whose message is the
        reason) when it cannot.
        """

    def is_running(self, session_id: str) -> bool: ...

    def publish_automation(self, frame: dict[str, Any]) -> None: ...


class AutomationRunner:
    """Turns "due at this moment" into sessions, on a thread or on demand."""

    def __init__(
        self,
        desk: Desk,
        store: Callable[[], AutomationStore],
        *,
        clock: Callable[[], float] = time.time,
        interval: float = TICK_SECONDS,
        command_runner: CommandRunner = run_watch_command,
    ) -> None:
        self._desk = desk
        self._store = store
        self._clock = clock
        self._interval = interval
        self._command_runner = command_runner
        #: One tick, run-now or check-now at a time: two of them acting on the
        #: same due schedule would start it twice.
        self._work = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._loop, name="autose-automation", daemon=True
        )
        self._thread.start()

    def stop(self, *, wait: float = 2.0) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=wait)

    def _loop(self) -> None:
        # Tick first: a service restarted after a night off runs what fell due
        # while it was down now, not half a minute from now.
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001, S110 - the loop must outlive one bad tick
                # A store that cannot be written or a desk mid-reload must
                # not end automation for the rest of the process; the next
                # tick tries again.
                pass
            self._stopping.wait(self._interval)

    # -- work -------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """Act on everything due now. Returns (and publishes) what happened."""
        with self._work:
            store = self._store()
            now = self._clock()
            due = store.due(now)
            report = _Report(now)
            # Each item is independent: one deleted from the dashboard while
            # this tick was under way is passed over, not the end of the tick.
            for schedule in due.schedules:
                with contextlib.suppress(UnknownAutomationError):
                    self._start_schedule(store, schedule, now, report)
            for watcher in due.pending:
                with contextlib.suppress(UnknownAutomationError):
                    self._start_watcher(
                        store, watcher, watcher.pending_prompt, now, report
                    )
            for watcher in due.checks:
                with contextlib.suppress(UnknownAutomationError):
                    outcome = store.check_watcher(
                        watcher.id, now=now, runner=self._command_runner
                    )
                    report.checked.append(_check_item(watcher, outcome))
                    if outcome.triggered:
                        self._start_watcher(store, watcher, outcome.prompt, now, report)
        return report.publish(self._desk)

    def run_schedule_now(self, schedule_id: str) -> dict[str, Any]:
        """Start a schedule's prompt now, whatever its cadence says."""
        with self._work:
            store = self._store()
            now = self._clock()
            schedule = store.schedule(schedule_id)
            report = _Report(now, manual=True)
            self._start_schedule(store, schedule, now, report)
        report.publish(self._desk)
        if report.skipped:
            raise SkippedRun(report.skipped[0]["reason"])
        return {"ran": report.ran[0], "schedule": store.schedule(schedule_id).to_dict()}

    def check_watcher_now(self, watcher_id: str) -> dict[str, Any]:
        """Run a watcher's check now; start its prompt if the check is news."""
        with self._work:
            store = self._store()
            now = self._clock()
            watcher = store.watcher(watcher_id)
            report = _Report(now, manual=True)
            outcome = store.check_watcher(watcher.id, now=now, runner=self._command_runner)
            report.checked.append(_check_item(watcher, outcome))
            if outcome.triggered:
                self._start_watcher(store, watcher, outcome.prompt, now, report)
        report.publish(self._desk)
        current = store.watcher(watcher_id)
        return {
            "check": outcome.to_dict(),
            "ran": report.ran[0] if report.ran else None,
            "skipped": report.skipped[0] if report.skipped else None,
            "watcher": current.to_dict(trusted=store.trusted(current)),
        }

    def _start_schedule(
        self, store: AutomationStore, schedule: Schedule, now: float, report: _Report
    ) -> None:
        item = {"type": "schedule", "id": schedule.id, "name": schedule.name}
        if schedule.last_session and self._desk.is_running(schedule.last_session):
            # Its previous run is still going. A second copy beside it would
            # race it over the same files.
            reason = "its previous run is still working"
            store.record_schedule_skipped(schedule.id, reason=reason)
            report.skipped.append({**item, "reason": reason})
            return
        try:
            session = self._desk.start_unattended(
                title=schedule.name,
                prompt=schedule.prompt,
                tier=schedule.tier or None,
                origin=item,
            )
        except Exception as exc:  # noqa: BLE001 - the reason is recorded
            reason = str(exc) or type(exc).__name__
            store.record_schedule_skipped(schedule.id, reason=reason)
            report.skipped.append({**item, "reason": reason})
            return
        store.record_schedule_run(schedule.id, now=now, session=session)
        report.ran.append({**item, "session": session})

    def _start_watcher(
        self,
        store: AutomationStore,
        watcher: Watcher,
        prompt: str,
        now: float,
        report: _Report,
    ) -> None:
        item = {"type": "watcher", "id": watcher.id, "name": watcher.name}
        try:
            session = self._desk.start_unattended(
                title=watcher.name,
                prompt=prompt,
                tier=watcher.tier or None,
                origin=item,
            )
        except Exception as exc:  # noqa: BLE001 - the reason is recorded
            reason = str(exc) or type(exc).__name__
            store.record_watcher_skipped(watcher.id, reason=reason)
            report.skipped.append({**item, "reason": reason})
            return
        store.record_watcher_started(watcher.id, now=now, session=session)
        report.ran.append({**item, "session": session})


class _Report:
    """What one tick (or one manual run) did, as the ``automation`` frame."""

    def __init__(self, now: float, *, manual: bool = False) -> None:
        self.now = now
        self.manual = manual
        self.ran: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.checked: list[dict[str, Any]] = []

    def frame(self) -> dict[str, Any]:
        return {
            "kind": "automation",
            "at": self.now,
            "manual": self.manual,
            "ran": self.ran,
            "skipped": self.skipped,
            "checked": self.checked,
        }

    def publish(self, desk: Desk) -> dict[str, Any]:
        frame = self.frame()
        # A quiet tick — nothing due, or only checks that found no news — is
        # not worth a frame every thirty seconds; a manual action always is,
        # because someone is waiting to see it.
        if self.manual or self.ran or self.skipped or any(
            item["outcome"] != "no change" for item in self.checked
        ):
            desk.publish_automation(frame)
        return frame


def _check_item(watcher: Watcher, outcome: WatchOutcome) -> dict[str, Any]:
    if outcome.refused:
        label = f"refused: {outcome.refused}"
    elif outcome.baseline:
        label = "baseline recorded"
    elif outcome.triggered:
        label = "triggered"
    else:
        label = "no change"
    return {
        "type": "watcher",
        "id": watcher.id,
        "name": watcher.name,
        "outcome": label,
        "exit_code": outcome.exit_code,
    }
