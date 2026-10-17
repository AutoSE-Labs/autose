"""Structured events emitted by the logic layer.

Events are the one-way channel from ``logic`` to whatever Adapter is driving
it (DR-002): the TUI renders them as terminal output, the GUI bridge
serialises them as JSON, a test collects them in a list. ``logic`` never
imports an Adapter to tell it something happened — it emits an event and the
Adapter decides what that looks like.

DR-012 additionally makes two of these events (``approval_requested`` /
``approval_resolved``) an *audit* requirement: a session's event log must show
what was asked and what was decided, independently of whether the transcript
itself is kept.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Event",
    "EventSink",
    "NullEventSink",
    "RecordingEventSink",
    "CallbackEventSink",
    "CompositeEventSink",
]


@dataclass(frozen=True)
class Event:
    """One thing that happened, in a form any Adapter can render or serialise."""

    type: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "data": dict(self.data),
            "timestamp": self.timestamp,
        }


@runtime_checkable
class EventSink(Protocol):
    """Strategy seam: where emitted events go.

    A new destination (a log file, a websocket, a CI annotation stream) is
    added by writing one implementation of this Protocol, with no edit to any
    emitting code.
    """

    def emit(self, event: Event) -> None:
        """Deliver *event*. Must not raise, and must not block indefinitely."""


class NullEventSink:
    """Discards every event. The default when an Adapter supplies none."""

    def emit(self, event: Event) -> None:  # Protocol implementation.
        return None


class RecordingEventSink:
    """Keeps every event in memory. Used by tests and by session recorders."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:  # Protocol implementation.
        self.events.append(event)

    def of_type(self, event_type: str) -> list[Event]:
        return [event for event in self.events if event.type == event_type]

    def to_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


class CallbackEventSink:
    """Forwards each event to a callable, swallowing Adapter-side failures.

    A broken renderer must never take down a run that is otherwise fine, so a
    raising callback is dropped rather than propagated back into ``logic``.
    """

    def __init__(self, callback) -> None:
        self._callback = callback

    def emit(self, event: Event) -> None:  # Protocol implementation.
        try:
            self._callback(event)
        except Exception:  # noqa: BLE001 - see docstring
            return None


class CompositeEventSink:
    """Fans one event out to several sinks (e.g. record it *and* render it)."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = tuple(sinks)

    def emit(self, event: Event) -> None:  # Protocol implementation.
        for sink in self._sinks:
            sink.emit(event)
