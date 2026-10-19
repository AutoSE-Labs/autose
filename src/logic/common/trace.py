"""What one turn did, step by step, kept with the turn (DR-055).

An Adapter sees a run as a stream of events: every tool call with its
arguments and result, every verdict and send-back, every model round's token
count, every stage starting and ending. The dashboard drew its Steps and
Usage views from that stream alone (DR-037, DR-041), so the record existed
only in the browser tab that watched the run: a reload, or any run from
before it, showed nothing.

This module keeps a compact copy on the logic side, where every Adapter's
sessions get it:

* :class:`TraceRecorder` is an :class:`EventSink`. The session arms it when a
  turn starts and takes the finished :class:`RunTrace` when the turn ends. It
  listens to the events the turn already emits; nothing that emits them knows
  it is there.
* :class:`RunTrace` is that record as plain data, stored on the turn and in
  the session file.

It is a record for a person to read, never context for a model: nothing reads
a trace back into a prompt or into ``SessionMemory``, which is what keeps it
on the right side of DR-004's boundary. It is bounded the way DR-026 bounds
stored change content — long strings trimmed, a cap on steps and rounds per
turn — and says how much it dropped rather than dropping it silently.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .events import Event

__all__ = [
    "RunTrace",
    "TraceRecorder",
    "MAX_TRACE_STEPS",
    "MAX_TRACE_ROUNDS",
    "TRACE_PREVIEW_CHARS",
    "TRACE_ARGUMENT_CHARS",
]

#: Steps (tool calls and notes) kept per turn. Past it the first half is kept
#: and then the most recent, with a count of the ones between: the start says
#: how the run set out, the end how it finished or why it stopped.
MAX_TRACE_STEPS = 200

#: Model rounds kept per turn, on the same head-and-tail rule. A round is a
#: handful of numbers, so the cap is generous.
MAX_TRACE_ROUNDS = 400

#: Stage passes and ledger snapshots kept per turn. A run that loops back
#: dozens of times is already its own story; the passes past this add little.
MAX_TRACE_STAGES = 60

#: Characters of a tool's result kept: the start and the end of the preview
#: the live event carried (DR-037), because the end is where a short test run
#: or build says how it went. The live view shows more; this is what is worth
#: keeping on disk.
TRACE_PREVIEW_CHARS = 600
_PREVIEW_TAIL_CHARS = 200

#: Characters of one argument value kept. `write_file`'s `content` is a whole
#: file; the change itself is in the turn's change record (DR-024).
TRACE_ARGUMENT_CHARS = 200

#: Characters of a note's message kept.
_MESSAGE_CHARS = 400

#: How deep and how wide a nested argument is kept.
_MAX_DEPTH = 3
_MAX_ITEMS = 20

#: The notable moments kept as notes, by event type, with the event data each
#: keeps. The event type is the note's ``kind``, so an Adapter words a stored
#: note exactly as it words the live event.
_NOTE_FIELDS: dict[str, tuple[str, ...]] = {
    "classification_finished": ("tier", "effective_tier", "ratcheted"),
    "stage_retry": ("attempt",),
    "stage_finished": (),
    "validation_verdict": ("passed", "overridden", "target", "unmet"),
    "loopback": ("target", "iteration"),
    "approval_resolved": ("tool", "approved", "reason"),
    "agent_nudged": ("attempt",),
    "tool_call_repeated": ("tool", "count"),
    "tool_call_malformed": ("attempt",),
    "agent_stalled": (),
    "agent_context_thrashing": (),
    "agent_round_cap_reached": (),
    "stop_requested": (),
    "agent_cancelled": ("rounds",),
    "context_truncated_by_runtime": (),
}

#: Every key a stored step may carry. Anything else in a file is dropped on
#: read, so a hand-edited session cannot put arbitrary fields in front of an
#: Adapter.
_TOOL_KEYS = frozenset(
    {
        "type", "id", "tool", "stage", "args", "status", "elapsed_ms",
        "preview", "preview_trimmed", "truncated", "artifact", "reason",
    }
)
_NOTE_KEYS = frozenset(
    {"type", "kind", "stage", "message", "title", "iteration", "count", "unmet", "agent"}
    | {key for keys in _NOTE_FIELDS.values() for key in keys}
)

#: What a stored step's fields may hold, by type. A session file sits in the
#: workspace, where a repository can put one, and an Adapter puts these values
#: into its markup: a status that is not a status, or a list that is a string,
#: is dropped on read rather than handed on (DR-055).
_STEP_STATUSES = frozenset({"done", "failed", "refused"})
_FLAG_KEYS = frozenset(
    {"preview_trimmed", "truncated", "passed", "overridden", "ratcheted", "approved"}
)
_NUMBER_KEYS = frozenset({"elapsed_ms", "iteration", "count", "attempt", "rounds"})


@dataclass(frozen=True)
class RunTrace:
    """One turn's steps, model rounds and stage times, as plain data."""

    #: When the turn started and ended (epoch seconds).
    started_at: float = 0.0
    finished_at: float = 0.0
    #: The workflow's stages in order: ``{"stage", "title"}``.
    stages: tuple[dict[str, str], ...] = ()
    #: Every pass through a stage: ``{"stage", "start", "end"}``.
    stage_times: tuple[dict[str, Any], ...] = ()
    #: Tool calls (``type: "tool"``) and notes (``type: "note"``), in order.
    steps: tuple[dict[str, Any], ...] = ()
    #: Every model round: ``{"stage", "prompt", "completion", "estimated",
    #: "limit"}`` — the same figures as DR-033's ``round_usage``.
    rounds: tuple[dict[str, Any], ...] = ()
    #: The requirements ledger as each stage left it: ``{"title",
    #: "statuses": {id: status}}`` (DR-038). Statuses only; the final ledger
    #: with descriptions and evidence is the turn's own ``requirements``.
    ledger: tuple[dict[str, Any], ...] = ()
    #: Steps left out, and where: ``steps[:dropped_after]`` came before them.
    dropped_steps: int = 0
    dropped_after: int = 0
    dropped_rounds: int = 0

    @property
    def has_steps(self) -> bool:
        """Whether there is anything to show beyond the turn's own totals."""
        return bool(self.steps or self.rounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages": [dict(item) for item in self.stages],
            "stage_times": [dict(item) for item in self.stage_times],
            "steps": [dict(item) for item in self.steps],
            "rounds": [dict(item) for item in self.rounds],
            "ledger": [
                {"title": item.get("title", ""), "statuses": dict(item.get("statuses", {}))}
                for item in self.ledger
            ],
            "dropped_steps": self.dropped_steps,
            "dropped_after": self.dropped_after,
            "dropped_rounds": self.dropped_rounds,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> RunTrace:
        """Read a stored trace, keeping only known fields, within the caps."""
        steps = [
            step
            for step in (_step_from(item) for item in _items(document.get("steps")))
            if step is not None
        ]
        rounds = [_round_from(item) for item in _items(document.get("rounds"))]
        dropped_steps = _int(document.get("dropped_steps"))
        dropped_after = min(_int(document.get("dropped_after")), len(steps))
        dropped_rounds = _int(document.get("dropped_rounds"))
        if len(steps) > MAX_TRACE_STEPS:
            dropped_steps += len(steps) - MAX_TRACE_STEPS
            dropped_after = MAX_TRACE_STEPS
            steps = steps[:MAX_TRACE_STEPS]
        if len(rounds) > MAX_TRACE_ROUNDS:
            dropped_rounds += len(rounds) - MAX_TRACE_ROUNDS
            rounds = rounds[:MAX_TRACE_ROUNDS]
        return cls(
            started_at=_float(document.get("started_at")),
            finished_at=_float(document.get("finished_at")),
            stages=tuple(
                {"stage": _text(item.get("stage"), 80), "title": _text(item.get("title"), 80)}
                for item in _items(document.get("stages"))[:MAX_TRACE_STAGES]
            ),
            stage_times=tuple(
                {
                    "stage": _text(item.get("stage"), 80),
                    "start": _float(item.get("start")),
                    "end": _float(item.get("end")),
                }
                for item in _items(document.get("stage_times"))[:MAX_TRACE_STAGES]
            ),
            steps=tuple(steps),
            rounds=tuple(rounds),
            ledger=tuple(
                {
                    "title": _text(item.get("title"), 80),
                    "statuses": {
                        _text(key, 40): _text(value, 40)
                        for key, value in list(_mapping(item.get("statuses")).items())[:100]
                    },
                }
                for item in _items(document.get("ledger"))[:MAX_TRACE_STAGES]
            ),
            dropped_steps=dropped_steps,
            dropped_after=dropped_after,
            dropped_rounds=dropped_rounds,
        )

    def without_detail(self) -> RunTrace:
        """This trace with its steps, rounds and ledger dropped, and counted.

        What a session keeps for an old turn once its stored traces are over
        their budget (see ``SessionRecord.bounded``): the stage times stay,
        being a few numbers, and the counts say what was there.
        """
        return replace(
            self,
            steps=(),
            rounds=(),
            ledger=(),
            dropped_steps=self.dropped_steps + len(self.steps),
            dropped_after=0,
            dropped_rounds=self.dropped_rounds + len(self.rounds),
        )


class _Kept:
    """A list held to *cap* items: the first half, then the most recent."""

    def __init__(self, cap: int) -> None:
        self._head_cap = max(0, cap) // 2
        self.head: list[dict[str, Any]] = []
        self.tail: deque[dict[str, Any]] = deque(maxlen=max(0, cap) - self._head_cap)
        self.dropped = 0

    def append(self, item: dict[str, Any]) -> None:
        if len(self.head) < self._head_cap:
            self.head.append(item)
            return
        if len(self.tail) == self.tail.maxlen:
            self.dropped += 1
        if self.tail.maxlen:
            self.tail.append(item)

    def last(self, match: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        item: dict[str, Any]
        for item in reversed(self.tail):
            if match(item):
                return item
        for item in reversed(self.head):
            if match(item):
                return item
        return None

    def items(self) -> list[dict[str, Any]]:
        return [*self.head, *self.tail]


class TraceRecorder:
    """Builds one turn's :class:`RunTrace` from the events it emits.

    An :class:`EventSink`, so it is wired in beside the Adapter's own sink and
    sees exactly what the Adapter sees. It records only between :meth:`start`
    and :meth:`finish`; an event outside a turn (a model switch, a stop asked
    for while idle) is not part of any run.

    Thread-safe: the dashboard asks a run to stop from another thread, and
    that request is one of the moments a trace keeps.
    """

    def __init__(
        self,
        *,
        max_steps: int = MAX_TRACE_STEPS,
        max_rounds: int = MAX_TRACE_ROUNDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_steps = max_steps
        self._max_rounds = max_rounds
        self._clock = clock
        self._lock = threading.Lock()
        self._active = False
        self._reset(0.0)
        self._handlers: dict[str, Callable[[Event], None]] = {
            "workflow_started": self._workflow_started,
            "stage_started": self._stage_started,
            "stage_finished": self._stage_finished,
            "tool_called": self._tool_called,
            "tool_completed": self._tool_completed,
            "approval_requested": self._approval_requested,
            "approval_resolved": self._approval_resolved,
            "round_usage": self._round_usage,
            "validation_verdict": self._verdict,
            "loopback": self._loopback,
            "agent_cancelled": self._cancelled,
        }

    # -- the turn's lifetime ----------------------------------------------

    def start(self) -> None:
        """Begin a new turn's trace, discarding anything left from the last."""
        with self._lock:
            self._reset(self._clock())
            self._active = True

    def finish(self) -> RunTrace | None:
        """End the turn and return its trace; ``None`` if none was started."""
        with self._lock:
            if not self._active:
                return None
            self._active = False
            now = self._clock()
            for item in self._stage_times:
                if not item["end"]:
                    # Ended with the turn: stopped, stalled, or out of budget.
                    item["end"] = now
            steps = self._steps.items()
            for step in steps:
                if step.get("status") == "running":
                    # Nothing came back before the turn ended.
                    step["status"] = "failed"
            return RunTrace(
                started_at=self._started_at,
                finished_at=now,
                stages=tuple(dict(item) for item in self._stages),
                stage_times=tuple(dict(item) for item in self._stage_times),
                steps=tuple(dict(step) for step in steps),
                rounds=tuple(dict(item) for item in self._rounds.items()),
                ledger=tuple(
                    {"title": item["title"], "statuses": dict(item["statuses"])}
                    for item in self._ledger
                ),
                dropped_steps=self._steps.dropped,
                dropped_after=len(self._steps.head) if self._steps.dropped else 0,
                dropped_rounds=self._rounds.dropped,
            )

    # -- EventSink ----------------------------------------------------------

    def emit(self, event: Event) -> None:  # Protocol implementation.
        with self._lock:
            if not self._active:
                return
            try:
                handler = self._handlers.get(event.type)
                if handler is not None:
                    handler(event)
                elif event.type in _NOTE_FIELDS:
                    self._note(event)
            except Exception:  # noqa: BLE001 - a record must never break a run
                return

    # -- internals ----------------------------------------------------------

    def _reset(self, started_at: float) -> None:
        self._started_at = started_at
        self._stage = ""
        self._stages: list[dict[str, str]] = []
        self._stage_times: list[dict[str, Any]] = []
        self._steps = _Kept(self._max_steps)
        self._rounds = _Kept(self._max_rounds)
        self._ledger: list[dict[str, Any]] = []
        self._calls = 0
        #: The call waiting for a person, so a refusal can name its arguments.
        self._asked: dict[str, Any] | None = None
        #: The last verdict's unmet requirements, which the send-back after it
        #: names (the event itself does not).
        self._unmet: list[str] = []

    def _note(self, event: Event, **extra: Any) -> None:
        data = event.data
        note: dict[str, Any] = {
            "type": "note",
            "kind": event.type,
            "stage": self._stage,
            "message": _text(event.message, _MESSAGE_CHARS),
        }
        for key in _NOTE_FIELDS.get(event.type, ()):
            if key in data:
                note[key] = _compact(data[key])
        note.update(extra)
        self._steps.append(note)

    def _workflow_started(self, event: Event) -> None:
        self._stages = [
            {"stage": str(name), "title": ""}
            for name in list(event.data.get("stages") or ())[:MAX_TRACE_STAGES]
        ]

    def _stage_started(self, event: Event) -> None:
        data = event.data
        self._stage = str(data.get("stage") or "")
        title = _text(data.get("title"), 80)
        known = next((item for item in self._stages if item["stage"] == self._stage), None)
        if known is None and len(self._stages) < MAX_TRACE_STAGES:
            self._stages.append({"stage": self._stage, "title": title})
        elif known is not None and title:
            known["title"] = title
        if len(self._stage_times) < MAX_TRACE_STAGES:
            self._stage_times.append(
                {"stage": self._stage, "start": event.timestamp, "end": 0.0}
            )
        iteration = _int(data.get("iteration"))
        if iteration > 1:
            # Back in a stage it has been through: the live view says so too.
            self._note(event, title=title, iteration=iteration)

    def _stage_finished(self, event: Event) -> None:
        stage = str(event.data.get("stage") or "")
        for item in reversed(self._stage_times):
            if item["stage"] == stage and not item["end"]:
                item["end"] = event.timestamp
                break
        requirements = event.data.get("requirements") or ()
        title = _text(event.data.get("title") or event.message, 80)
        if requirements and len(self._ledger) < MAX_TRACE_STAGES:
            self._ledger.append(
                {
                    "title": title,
                    "statuses": {
                        _text(item.get("id"), 40): _text(item.get("status"), 40)
                        for item in requirements
                        if isinstance(item, Mapping)
                    },
                }
            )
        self._note(event, title=title)

    def _tool_called(self, event: Event) -> None:
        data = event.data
        self._calls += 1
        self._asked = None
        self._steps.append(
            {
                "type": "tool",
                "id": _text(data.get("call_id"), 80) or f"call-{self._calls}",
                "tool": _text(data.get("tool"), 80),
                "stage": self._stage,
                "args": _compact(_mapping(data.get("arguments"))),
                "status": "running",
            }
        )

    def _tool_completed(self, event: Event) -> None:
        data = event.data
        call_id = _text(data.get("call_id"), 80)
        tool = _text(data.get("tool"), 80)
        # Matched by call id; without one, the newest running call of the tool.
        step = self._steps.last(
            lambda item: item.get("type") == "tool"
            and item.get("status") == "running"
            and (item.get("id") == call_id if call_id else item.get("tool") == tool)
        )
        if step is None:
            return
        preview, trimmed = _preview(str(data.get("preview") or ""))
        step["status"] = "failed" if data.get("failed") else "done"
        step["elapsed_ms"] = _int(data.get("elapsed_ms"))
        step["preview"] = preview
        step["preview_trimmed"] = trimmed
        step["truncated"] = bool(data.get("truncated"))
        step["artifact"] = _text(data.get("artifact"), 200)

    def _approval_requested(self, event: Event) -> None:
        self._asked = {
            "tool": _text(event.data.get("tool"), 80),
            "args": _compact(_mapping(event.data.get("arguments"))),
        }

    def _approval_resolved(self, event: Event) -> None:
        data = event.data
        asked, self._asked = self._asked, None
        if not data.get("approved"):
            # A refused call never reaches the executor's `tool_called`, so
            # without this it would be in the record only as a sentence.
            tool = _text(data.get("tool"), 80)
            self._calls += 1
            self._steps.append(
                {
                    "type": "tool",
                    "id": f"refused-{self._calls}",
                    "tool": tool,
                    "stage": self._stage,
                    "args": asked["args"] if asked and asked["tool"] == tool else {},
                    "status": "refused",
                    "reason": _text(data.get("reason"), _MESSAGE_CHARS),
                }
            )
        self._note(event)

    def _round_usage(self, event: Event) -> None:
        data = event.data
        self._rounds.append(
            {
                "stage": _text(data.get("stage"), 80),
                "prompt": _int(data.get("prompt_tokens")),
                "completion": _int(data.get("completion_tokens")),
                "estimated": _int(data.get("estimated_input_tokens")),
                "limit": _int(data.get("context_limit")),
            }
        )

    def _verdict(self, event: Event) -> None:
        self._unmet = [str(item) for item in event.data.get("unmet") or ()]
        self._note(event)

    def _loopback(self, event: Event) -> None:
        self._note(event, unmet=_compact(self._unmet))

    def _cancelled(self, event: Event) -> None:
        # The agent's name, which is what the live view says stopped.
        self._note(event, agent=_text(event.data.get("stage"), 80))


# -- trimming ---------------------------------------------------------------


def _preview(text: str) -> tuple[str, bool]:
    """The start and the end of a result, and whether anything was cut."""
    if len(text) <= TRACE_PREVIEW_CHARS:
        return text, False
    head = TRACE_PREVIEW_CHARS - _PREVIEW_TAIL_CHARS
    return f"{text[:head].rstrip()}\n…\n{text[-_PREVIEW_TAIL_CHARS:].lstrip()}", True


def _compact(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy of *value* with long strings and collections trimmed."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN and the infinities are not JSON: a page could not read them back.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _text(value, TRACE_ARGUMENT_CHARS)
    if depth >= _MAX_DEPTH:
        return _text(str(value), TRACE_ARGUMENT_CHARS)
    if isinstance(value, Mapping):
        return {
            _text(key, 80): _compact(item, depth + 1)
            for key, item in list(value.items())[:_MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_compact(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    return _text(str(value), TRACE_ARGUMENT_CHARS)


def _text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _step_from(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    kind = item.get("type")
    if kind == "tool":
        step = {key: _field(key, value) for key, value in item.items() if key in _TOOL_KEYS}
        step["args"] = _compact(_mapping(item.get("args")))
        # Only the statuses a finished trace holds: a call still "running"
        # when the turn ended was closed as failed (TraceRecorder.finish).
        if step.get("status") not in _STEP_STATUSES:
            step["status"] = "failed"
        if "preview" in item:
            step["preview"], cut = _preview(str(item.get("preview") or ""))
            step["preview_trimmed"] = item.get("preview_trimmed") is True or cut
        return step
    if kind == "note":
        note = {key: _field(key, value) for key, value in item.items() if key in _NOTE_KEYS}
        note["message"] = _text(item.get("message"), _MESSAGE_CHARS)
        return note
    return None


def _field(key: str, value: Any) -> Any:
    """A stored step's field, as the type the recorder wrote it."""
    if key in _FLAG_KEYS:
        return value is True
    if key in _NUMBER_KEYS:
        return _int(value)
    if key == "unmet":
        items = value if isinstance(value, (list, tuple)) else ()
        return [_text(entry, 80) for entry in list(items)[:_MAX_ITEMS]]
    if key == "args":
        return _compact(_mapping(value))
    return _text(value, _MESSAGE_CHARS if key in {"message", "reason"} else TRACE_ARGUMENT_CHARS)


def _round_from(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": _text(item.get("stage"), 80),
        "prompt": _int(item.get("prompt")),
        "completion": _int(item.get("completion")),
        "estimated": _int(item.get("estimated")),
        "limit": _int(item.get("limit")),
    }


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0
