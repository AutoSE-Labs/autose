"""Several ``logic.session.Session`` objects, driven from one browser (DR-002).

The dashboard is HTTP, and HTTP is a series of short unrelated requests; a turn
is a long blocking generator that occasionally stops to ask a question. This
module is the whole of that translation, and it is presentation-side by
construction: it starts each turn on its own worker thread, fans the events
``logic`` emits out to every connected browser, parks an approval request until
someone answers it, and refuses anything that would mutate a session while that
session's own turn is still running.

Four properties are load-bearing rather than incidental:

* **Fail-closed approval (DR-012).** An approval nobody answers is denied, not
  waited on forever — a closed tab must not leave a shell command pending.
* **One turn per session, many sessions at once (DR-031).** ``Session`` is not
  concurrent, so a second prompt *into the same session* is refused with a
  conflict rather than queued into a race. A prompt into a different open
  session gets its own thread and runs beside the first, which is what keeps
  the window usable while something long is generating.
* **A busy session never freezes the rest of the dashboard.** Everything that
  used to be gated on "is anything running" is gated on "is *this* session
  running"; opening, resuming, reading and switching sessions are never
  refused. The two things that genuinely cannot be done beside a run — undo,
  which rewrites files another run may be editing, and a config reload, which
  rebuilds every session — say so and name what to wait for.
* **A late browser misses nothing.** Frames are numbered, tagged with the
  session they belong to, and briefly retained, so a reload mid-turn replays
  the activity it did not see instead of showing an empty card until the next
  event happens to arrive.
"""

from __future__ import annotations

import contextlib
import queue
import secrets
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from logic import __version__
from logic.common.approval import (
    APPROVAL_REQUIRED_TOOLS,
    EDIT_APPROVAL_TOOLS,
    ApprovalDecision,
    ApprovalRequest,
    CallbackApprovalGate,
)
from logic.common.context import MIN_USABLE_CONTEXT_TOKENS
from logic.common.events import CallbackEventSink, Event, EventSink
from logic.config import AutoSEConfig, load_config
from logic.connections import list_connections
from logic.credentials import CredentialStore
from logic.diagnostics import diagnose
from logic.errors import AutoSEError, ConfigurationError
from logic.inference.catalog import ModelInfo
from logic.inference.context_advice import advise_context, window_choices
from logic.inference.registry import get_provider
from logic.persistence import SESSIONS_DIRECTORY, FileSessionStore, SessionRecord
from logic.session import (
    Session,
    SessionSetup,
    serves_requested_window,
    setup_for_provider,
    setup_from_config,
)
from logic.tiers import SPARK, is_tier
from logic.user_config import save_default_inference
from logic.workflows import UnavailableTierError

from .payloads import (
    change_payload,
    connection_payload,
    diagnosis_payload,
    event_payload,
    model_payload,
    record_payload,
    revert_payload,
    runtime_payload,
    session_title,
    tier_payloads,
    turn_payload,
    warnings_payload,
)

__all__ = [
    "BridgeError",
    "SessionBridge",
    "APPROVAL_TIMEOUT_SECONDS",
    "MAX_OPEN_SESSIONS",
]

#: How long an approval-required call waits for a human before it is denied.
#: Generous — someone may have walked away mid-task — but finite, because
#: DR-012's default is refusal and a browser that never answers is a browser
#: that is not there.
APPROVAL_TIMEOUT_SECONDS = 900.0

#: How many sessions may be open on the desk at once (DR-031). Every one of
#: them can be generating, and every generation is a worker thread holding a
#: model connection; past a handful they only slow each other down. Closing one
#: does not lose it — it is on disk and reopens from the rail.
MAX_OPEN_SESSIONS = 6

#: Frames kept for a browser that reconnects mid-turn. A busy staged run emits
#: a few hundred; this is one turn's worth of catching up, not a log.
_FRAME_HISTORY = 600

#: Frames a single browser may fall behind by before it is told to resynchronise
#: from ``/api/state`` instead of being fed an ever-growing backlog.
_QUEUE_LIMIT = 2_000

#: Events kept per running turn so a page opened mid-turn can rebuild the work
#: card (DR-033). Streaming deltas are left out — they are a few hundred per
#: stage and the card keeps only their tail — so this holds the stages, tool
#: calls, approvals and verdicts of even a long run.
_TIMELINE_LIMIT = 400

#: Characters of streamed stage output and reasoning kept for that same page.
_STREAM_TAIL = 6_000

#: Event types that are deltas of a stream rather than steps of the run.
_STREAMED_EVENTS = frozenset({"agent_reasoning_delta", "stage_output_delta"})

#: More than a handful of dashboards on one session is a bug, not a use case:
#: every subscriber is a thread parked on a socket.
_MAX_SUBSCRIBERS = 8


class BridgeError(Exception):
    """Something the dashboard asked for cannot be done, and why.

    Carries the HTTP status so the browser can tell "you are asking at the
    wrong time" (409, retry later) apart from "that is not a thing" (400).
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _Subscriber:
    """One connected browser's slice of the frame stream."""

    def __init__(self) -> None:
        self.frames: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue(
            maxsize=_QUEUE_LIMIT
        )
        #: Set when a frame had to be dropped. The stream then tells the
        #: browser to refetch state rather than pretending it is up to date.
        self.lagged = False


class _Bus:
    """Numbered fan-out of dashboard frames, with a short replay window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[_Subscriber] = []
        self._history: deque[tuple[int, dict[str, Any]]] = deque(maxlen=_FRAME_HISTORY)
        self._sequence = 0

    def publish(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            entry = (self._sequence, frame)
            self._history.append(entry)
            for subscriber in self._subscribers:
                try:
                    subscriber.frames.put_nowait(entry)
                except queue.Full:
                    subscriber.lagged = True

    def subscribe(self, *, since: int = 0) -> _Subscriber:
        """Attach a browser, replaying whatever it has not seen.

        *since* is the last frame id it received — 0 from a browser that has
        seen nothing. A page that has just read ``/api/state`` passes the
        ``sequence`` that came with it, so the catch-up starts where the
        snapshot ended rather than replaying a turn it already drew.
        """
        subscriber = _Subscriber()
        with self._lock:
            if len(self._subscribers) >= _MAX_SUBSCRIBERS:
                raise BridgeError(
                    "too many dashboards are connected to this session", status=503
                )
            for entry in self._history:
                if entry[0] > since:
                    try:
                        subscriber.frames.put_nowait(entry)
                    except queue.Full:  # pragma: no cover - history < limit
                        subscriber.lagged = True
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence


class _Pending:
    """One approval-required call, parked until a human answers it (DR-012)."""

    def __init__(self, request: ApprovalRequest) -> None:
        self.id = secrets.token_hex(8)
        self.request = request
        self.created_at = time.time()
        self.answered = threading.Event()
        self.decision: ApprovalDecision | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            **self.request.to_dict(),
        }


class _Live:
    """One session open on the desk, with the turn state that belongs to it.

    Plain state, deliberately: the bridge owns every decision about it, so the
    two objects do not reach into each other. The event sink and approval gate
    are handed in already bound to this session, which is how a frame the
    browser receives knows which card it belongs to.
    """

    def __init__(self, session: Session, *, events: EventSink) -> None:
        self.session = session
        self.events = events
        self.running = False
        self.current: dict[str, Any] | None = None
        self.answer: list[str] = []
        self.pending: dict[str, _Pending] = {}
        self.opened_at = time.time()
        # DR-033: what the current turn has done so far, for a page that opens
        # after it started. Reset by every new turn.
        self.timeline: list[dict[str, Any]] = []
        self.narration = ""
        self.thinking = ""
        #: The models of the provider this session switched to (DR-036), for
        #: the picker; ``None`` while it is still on the default provider.
        self.models: tuple[ModelInfo, ...] | None = None

    def reset_turn(self) -> None:
        self.answer = []
        self.timeline = []
        self.narration = ""
        self.thinking = ""

    def record(self, payload: dict[str, Any]) -> None:
        """Keep one event of the running turn, bounded (DR-033)."""
        kind = payload.get("event")
        message = str(payload.get("message") or "")
        if kind == "stage_output_delta":
            self.narration = (self.narration + message)[-_STREAM_TAIL:]
        elif kind == "agent_reasoning_delta":
            self.thinking = (self.thinking + message)[-_STREAM_TAIL:]
        elif kind == "agent_reasoning":
            self.thinking = (self.thinking + message + "\n")[-_STREAM_TAIL:]
        if kind in _STREAMED_EVENTS:
            return
        if kind == "stage_started":
            # The card shows the running stage's output, not the last one's.
            self.narration = ""
        if len(self.timeline) >= _TIMELINE_LIMIT:
            # Keep the head — `workflow_started` names the stages — and drop
            # the oldest of the rest.
            del self.timeline[1]
        self.timeline.append(payload)

    @property
    def id(self) -> str:
        return self.session.session_id


class SessionBridge:
    """Everything the HTTP layer is allowed to do to the open sessions."""

    def __init__(
        self,
        config: AutoSEConfig,
        *,
        config_path: str | Path | None = None,
        default_tier: str | None = None,
        resume: str | None = None,
        approval_timeout: float = APPROVAL_TIMEOUT_SECONDS,
        credentials: CredentialStore | None = None,
    ) -> None:
        self._config = config
        #: API keys (DR-036). Written from the settings page, read when a
        #: session switches provider; never put into a payload.
        self._credentials = credentials or CredentialStore()
        self._config_path = config_path
        self._default_tier = default_tier
        self._approval_timeout = approval_timeout
        self._lock = threading.RLock()
        self._bus = _Bus()
        #: Open sessions, in the order they were opened. Insertion order is the
        #: rail's order, so a session does not jump around as it runs.
        self._open: dict[str, _Live] = {}
        #: Which one the browser is looking at. Server-side rather than per-tab
        #: because everything else here is shared too: two tabs on one
        #: dashboard show one desk.
        self._focus = ""
        self._setup: SessionSetup | None = None
        self._store = FileSessionStore(
            config.resolve_workspace_root() / SESSIONS_DIRECTORY
        )
        self._problem = ""
        self._resolve(resume=resume)

    # -- lifecycle ---------------------------------------------------------

    def _resolve(self, *, resume: str | None = None) -> None:
        """Find a runtime and open a session, or record why there is none.

        A dashboard that refused to open because Ollama was not running yet
        would be the worst possible version of this: the user cannot read the
        explanation without the window the explanation is in. So discovery
        failing is a *state*, shown in the setup panel with a retry, rather
        than a startup error.
        """
        self._problem = ""
        try:
            setup = setup_from_config(self._config)
        except AutoSEError as exc:
            self._setup = None
            self._problem = str(exc)
            return
        self._setup = setup
        if not setup.is_ready:
            self._problem = setup.warnings[0] if setup.warnings else (
                "no local runtime is serving a model"
            )
            return
        record: SessionRecord | None = None
        if resume:
            record = (
                self._store.latest()
                if resume == "latest"
                else self._store.resolve(resume)
            )
            if record is None:
                self._problem = f"no stored session matches '{resume}'"
        self._open_session(record)

    def _open_session(self, record: SessionRecord | None) -> _Live | None:
        """Build a session, put it on the desk, and focus it.

        The event sink and approval gate are built *before* the session and
        closed over the ``_Live`` that will hold it, so every frame the session
        emits carries the id of the session it came from. Without that a second
        run's activity would land in the first run's card.
        """
        setup = self._setup
        assert setup is not None  # only called once a ready setup exists
        holder: list[_Live] = []

        def on_event(event: Event) -> None:
            if holder:
                self._on_event(holder[0], event)

        def on_approval(request: ApprovalRequest) -> ApprovalDecision:
            if not holder:  # pragma: no cover - no gate call before wiring
                return ApprovalDecision.deny("this session is not open yet")
            return self._ask(holder[0], request)

        events = CallbackEventSink(on_event)
        try:
            session = Session.from_setup(
                setup,
                events=events,
                # DR-012: auto-approve is honoured only because the operator
                # asked for it on the command line, and `from_setup` is what
                # attributes it. Otherwise every gated call reaches the browser.
                approval_gate=(
                    None
                    if self._config.auto_approve
                    else CallbackApprovalGate(on_approval)
                ),
                default_tier=self._default_tier or SPARK,
                auto_classify=self._default_tier is None,
                store=self._store,
                resume=record,
                credentials=self._credentials,
            )
        except AutoSEError as exc:
            self._problem = str(exc)
            return None
        live = _Live(session, events=events)
        holder.append(live)
        self._open[live.id] = live
        self._focus = live.id
        return live

    def reload(self) -> dict[str, Any]:
        """Re-read config, re-run discovery, and rebuild the desk.

        Every open session is closed first: the runtime, the model, or the
        workspace may all be different afterwards, and a session still holding
        a connection to the old one would be a lie about what it is running
        against. They are on disk, so the rail reopens any of them.
        """
        with self._lock:
            self._require_nothing_running("reload the configuration")
            try:
                self._config = _apply_overrides(
                    load_config(self._config_path, workspace_root=self._config.workspace_root),
                    self._config,
                )
            except ConfigurationError as exc:
                raise BridgeError(str(exc)) from exc
            self._store = FileSessionStore(
                self._config.resolve_workspace_root() / SESSIONS_DIRECTORY
            )
            self._open.clear()
            self._focus = ""
            self._resolve()
        return self._broadcast_state()

    def new_session(self) -> dict[str, Any]:
        """Open a fresh session beside whatever else is running (DR-031)."""
        with self._lock:
            if self._setup is None or not self._setup.is_ready:
                raise BridgeError("there is no runtime to start a session against")
            self._require_desk_room()
            if self._open_session(None) is None:
                raise BridgeError(self._problem or "could not start a session")
        return self._broadcast_state()

    def resume(self, reference: str) -> dict[str, Any]:
        """Open a stored session by id or unique prefix (DR-026).

        One that is already on the desk is focused rather than opened twice —
        two ``Session`` objects over one stored id would each persist over the
        other's history.
        """
        with self._lock:
            already = self._open.get(reference)
            if already is not None:
                self._focus = already.id
                return self._broadcast_state()
            record = (
                self._store.latest()
                if reference == "latest"
                else self._store.resolve(reference)
            )
            if record is None:
                raise BridgeError(
                    f"no stored session matches '{reference}'", status=404
                )
            existing = self._open.get(record.id)
            if existing is not None:
                self._focus = existing.id
                return self._broadcast_state()
            if self._setup is None or not self._setup.is_ready:
                raise BridgeError("there is no runtime to resume against")
            self._require_desk_room()
            if self._open_session(record) is None:
                raise BridgeError(self._problem or "could not resume that session")
        return self._broadcast_state()

    def focus(self, session_id: str) -> dict[str, Any]:
        """Look at another open session. Never refused — that is the point."""
        with self._lock:
            live = self._open.get(session_id)
            if live is None:
                raise BridgeError(
                    f"session '{session_id}' is not open", status=404
                )
            self._focus = live.id
        return self._broadcast_state()

    def close_session(self, session_id: str) -> dict[str, Any]:
        """Take a session off the desk, leaving it on disk.

        A running one is refused rather than abandoned: its worker thread would
        carry on writing files with nothing on screen accounting for it. Stop
        it first. Closing the last one opens a fresh session, so the dashboard
        is never left with nothing to type into.
        """
        with self._lock:
            live = self._open.get(session_id)
            if live is None:
                raise BridgeError(f"session '{session_id}' is not open", status=404)
            if live.running:
                raise BridgeError(
                    "that session is still working — stop it before closing it",
                    status=409,
                )
            self._deny_pending(live, "the session was closed")
            del self._open[session_id]
            if self._focus == session_id:
                self._focus = next(reversed(self._open), "")
            if not self._open and self._setup is not None and self._setup.is_ready:
                self._open_session(None)
        return self._broadcast_state()

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if session_id in self._open:
                raise BridgeError(
                    "that session is open — close it first",
                    status=409,
                )
            if not self._store.delete(session_id):
                raise BridgeError(f"no stored session '{session_id}'", status=404)
        return {"deleted": session_id, "sessions": self.sessions()}

    def shutdown(self) -> None:
        """Deny anything still parked, so no thread is left waiting."""
        with self._lock:
            live = list(self._open.values())
        for one in live:
            self._deny_pending(one, "the dashboard is shutting down")

    # -- state -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard needs to draw itself from cold.

        The focused session is flattened into the top level — that is the one
        the conversation column draws, and it is the shape every reader of this
        payload already expects. ``open`` carries a summary of each session on
        the desk, which is what the rail needs to show a run that is happening
        somewhere the user is not currently looking.
        """
        setup = self._setup
        with self._lock:
            live = self._open.get(self._focus)
            desk = [self._live_payload(one) for one in self._open.values()]
            focus = self._focus
            sequence = self._bus.sequence
            # Read with `sequence`: the timeline must end exactly where the
            # stream a page opens from this snapshot begins (DR-033).
            turn_view = (
                (list(live.timeline), live.narration, live.thinking, "".join(live.answer))
                if live is not None
                else ([], "", "", "")
            )
            running_elsewhere = [
                one.id for one in self._open.values() if one.running and one is not live
            ]
        session = live.session if live is not None else None
        inference = session.inference if session is not None else (
            setup.inference if setup is not None else None
        )
        state: dict[str, Any] = {
            "version": __version__,
            "ready": session is not None,
            "problem": self._problem,
            "workspace": str(self._config.resolve_workspace_root()),
            "config_source": str(self._config.source) if self._config.source else "",
            "sequence": sequence,
            "focus": focus,
            "open": desk,
            "max_open": MAX_OPEN_SESSIONS,
            "running_elsewhere": running_elsewhere,
            "tiers": tier_payloads(),
            "auto_approve": self._config.auto_approve,
            "authorized_by": self._config.auto_approve_authorized_by,
            "require_edit_approval": self._config.require_edit_approval,
            "gated_tools": sorted(
                APPROVAL_REQUIRED_TOOLS
                | (EDIT_APPROVAL_TOOLS if self._config.require_edit_approval else frozenset())
            ),
            "warnings": warnings_payload(
                self._config.warnings, setup.warnings if setup else ()
            ),
            "runtimes": [
                runtime_payload(runtime) for runtime in (setup.runtimes if setup else ())
            ],
            "provider": inference.provider if inference else "",
            "base_url": inference.base_url if inference else "",
            "model": inference.model if inference else "",
            # What a new session starts on (DR-036); the focused one may differ.
            "default": self._default_payload(),
        }
        if live is None or session is None:
            state.update(
                {
                    "session_id": "",
                    "tier": self._default_tier or SPARK,
                    "auto_classify": self._default_tier is None,
                    "running": False,
                    "stopping": False,
                    "current": None,
                    "answer": "",
                    "pending_approvals": [],
                    "context_limit": None,
                    "context_advice": None,
                    "model_info": None,
                    "models": [],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "history": [],
                    "changes": [],
                }
            )
            return state
        with self._lock:
            running = live.running
            current = dict(live.current) if live.current else None
            if current is not None:
                # DR-033: enough to redraw the work card from cold. Only the
                # focused session carries it; the rail's summaries stay small.
                current["timeline"], current["narration"], current["thinking"], _ = turn_view
            answer = turn_view[3] if running else ""
            pending = [item.payload() for item in live.pending.values()]
        history = session.history
        state.update(
            {
                "session_id": session.session_id,
                "tier": session.tier,
                "auto_classify": session.auto_classify,
                "running": running,
                # DR-030: a browser that reconnects mid-turn must not offer a
                # Stop button that has already been pressed.
                "stopping": bool(running and session.stop_requested),
                "current": current,
                "answer": answer,
                "pending_approvals": pending,
                "context_limit": session.context_limit(),
                # DR-034: one verdict about the served window, so the page can
                # tell a deliberate cap from a window that will hurt.
                "context_advice": _context_payload(session),
                "model_info": model_payload(session.model_info),
                "models": [
                    model_payload(info)
                    for info in (
                        live.models
                        if live.models is not None
                        else setup.available_models
                        if setup
                        else ()
                    )
                ],
                "usage": {
                    "prompt_tokens": session.usage.prompt_tokens,
                    "completion_tokens": session.usage.completion_tokens,
                    "total_tokens": session.usage.total_tokens,
                },
                "history": [
                    turn_payload(turn, index=index)
                    for index, turn in enumerate(history)
                ],
                "changes": [
                    change_payload(change) for change in session.last_changes
                ],
            }
        )
        return state

    def _live_payload(self, live: _Live) -> dict[str, Any]:
        """One session as the rail draws it: enough to see it working.

        Deliberately not its history. A desk of six sessions in one state frame
        would otherwise put six transcripts on the socket every time any one of
        them finished a turn; the conversation column only ever draws the
        focused one, and focusing sends its history then.
        """
        session = live.session
        return {
            "session_id": live.id,
            "title": session_title(session.history, live.current),
            "tier": session.tier,
            "auto_classify": session.auto_classify,
            "running": live.running,
            "stopping": bool(live.running and session.stop_requested),
            "current": dict(live.current) if live.current else None,
            "waiting": len(live.pending),
            "turns": len(session.history),
            "total_tokens": session.usage.total_tokens,
            "opened_at": live.opened_at,
            "focused": live.id == self._focus,
        }

    def sessions(self) -> dict[str, Any]:
        records = self._store.list(limit=50)
        with self._lock:
            open_ids = list(self._open)
            focus = self._focus
        return {
            "sessions": [record_payload(record) for record in records],
            "problems": list(self._store.problems),
            "open": open_ids,
            "current": focus,
        }

    def changes(self, session_id: str | None = None) -> dict[str, Any]:
        """The last turn's writes, with diffs — what ``/diff`` shows (DR-024)."""
        session = self._target(session_id).session
        changes = session.last_changes
        added = sum(change.stats[0] for change in changes)
        removed = sum(change.stats[1] for change in changes)
        return {
            "session": session.session_id,
            "changes": [
                change_payload(change, with_diff=True) for change in changes
            ],
            "added": added,
            "removed": removed,
        }

    def doctor(self) -> dict[str, Any]:
        """What this machine can run — ``autose --doctor`` as a panel."""
        return diagnosis_payload(diagnose(self._config))

    # -- mutation ----------------------------------------------------------

    def submit(
        self,
        prompt: str,
        *,
        tier: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a turn on a worker thread. Returns as soon as it is running."""
        text = prompt.strip()
        if not text:
            raise BridgeError("there is nothing to run — describe a request first")
        if tier and not is_tier(tier):
            raise BridgeError(f"'{tier}' is not a tier")
        with self._lock:
            live = self._target(session_id)
            session = live.session
            if live.running:
                raise BridgeError(
                    "a task is already running in this session", status=409
                )
            live.running = True
            live.reset_turn()
            live.current = {
                "prompt": text,
                "requested_tier": tier or "",
                "tier": tier or session.tier,
                "started_at": time.time(),
            }
            # DR-031: two runs over one workspace journal their writes
            # separately, so an edit one makes is invisible to the other's undo.
            # Worth saying once, at the moment it becomes true.
            beside = [one.id for one in self._open.values() if one.running and one is not live]
            worker = threading.Thread(
                target=self._work,
                args=(live, text, tier),
                name=f"autose-turn-{live.id[:8]}",
                daemon=True,
            )
        self._publish_for(
            live, {"kind": "busy", "running": True, "current": dict(live.current or {})}
        )
        if beside:
            self._publish(
                {
                    "kind": "concurrent",
                    "sessions": [live.id, *beside],
                    "workspace": str(self._config.resolve_workspace_root()),
                }
            )
        worker.start()
        return {"started": True, "session": live.id}

    def interrupt(self, session_id: str | None = None) -> dict[str, Any]:
        """Ask a running turn to stop at its next safe point (DR-030).

        The dashboard's counterpart to the terminal's ctrl-c, and the reason
        the stop signal is a token rather than an exception: the turn is on a
        worker thread, and this request arrives on the HTTP thread. Nothing is
        killed here — the worker unwinds, publishes its partial turn, and the
        browser sees it land like any other outcome.
        """
        with self._lock:
            live = self._target(session_id)
            if not live.running:
                raise BridgeError("nothing is running in this session", status=409)
        live.session.request_stop("you stopped this run from the dashboard")
        # A call parked on an approval would otherwise hold the turn until the
        # approval timeout: the user has said stop, which is an answer.
        self._deny_pending(live, "the user stopped this run before the call was made")
        self._publish_for(live, {"kind": "stopping"})
        return {"stopping": True, "session": live.id}

    def resolve_approval(
        self, approval_id: str, *, approved: bool, reason: str = ""
    ) -> dict[str, Any]:
        """Answer one parked approval-required call (DR-012).

        Found by id across every open session, because the browser answers the
        call it was shown and should not have to say which card it came from.
        """
        with self._lock:
            pending = None
            for live in self._open.values():
                pending = live.pending.get(approval_id)
                if pending is not None:
                    break
        if pending is None:
            # Already resolved, or timed out while the browser was deciding.
            raise BridgeError(
                "that approval is no longer waiting for an answer", status=409
            )
        pending.decision = (
            ApprovalDecision.allow(reason.strip())
            if approved
            else ApprovalDecision.deny(
                reason.strip() or "the user declined this call in the dashboard"
            )
        )
        pending.answered.set()
        return {"resolved": approval_id, "approved": approved}

    def select_model(
        self,
        name: str,
        *,
        session_id: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """Switch one session's model, on any provider with *provider* (DR-036).

        A model picked on the default provider without naming one still
        becomes the default for new sessions, as it always has. Crossing to
        another provider changes only this session; the settings page's
        ``set_default`` is how a provider becomes the default.
        """
        with self._lock:
            live = self._target(session_id)
            self._require_idle(live)
            session = live.session
            before = (session.inference.provider, session.inference.base_url)
            try:
                session.select_model(name, provider=provider, base_url=base_url)
            except AutoSEError as exc:
                raise BridgeError(str(exc)) from exc
            after = (session.inference.provider, session.inference.base_url)
            setup = self._setup
            on_default = (
                setup is not None
                and setup.inference is not None
                and after == (setup.inference.provider, setup.inference.base_url)
            )
            if after != before:
                live.models = None if on_default else session.available_models()
            elif on_default and live.models is None and setup is not None:
                self._setup = setup.with_model(name)
        return self._broadcast_state()

    # -- providers and keys (DR-036) ----------------------------------------

    def providers(self) -> dict[str, Any]:
        """Every registered provider: reachable or not, keyed or not, its models.

        Probed in parallel outside the bridge lock, so a slow hosted API never
        holds up a running session's frames. Keys appear only masked.
        """
        with self._lock:
            configured = self._config.inference
            urls = {configured.provider: configured.base_url}
            # A session using a provider at a non-default address is the
            # address worth listing for it.
            for live in self._open.values():
                inference = live.session.inference
                urls.setdefault(inference.provider, inference.base_url)
            keys = {configured.provider: configured.api_key}
            default = self._default_payload()
        connections = list_connections(
            credentials=self._credentials,
            base_urls=urls,
            configured_keys=keys,
            timeout=configured.discovery_timeout,
        )
        return {
            "providers": [connection_payload(one) for one in connections],
            "default": default,
            "credentials_file": str(self._credentials.path),
        }

    def save_key(self, provider: str, api_key: str) -> dict[str, Any]:
        """Store *provider*'s API key, owner-only, outside config (DR-036)."""
        name = self._registered(provider)
        try:
            self._credentials.save(name, api_key)
        except ConfigurationError as exc:
            raise BridgeError(str(exc)) from exc
        with self._lock:
            self._adopt_key(name, self._credentials.resolve(name))
            self._retry_if_idle()
        self._broadcast_state()
        return self.providers()

    def delete_key(self, provider: str) -> dict[str, Any]:
        """Forget *provider*'s stored key. Open sessions keep theirs until switched."""
        name = self._registered(provider)
        removed = self._credentials.stored(name)
        if not self._credentials.delete(name):
            raise BridgeError(f"no key is stored for {name}", status=404)
        with self._lock:
            if self._config.inference.api_key == removed:
                self._adopt_key(name, self._credentials.resolve(name))
        self._broadcast_state()
        return self.providers()

    def set_default(
        self, provider: str, model: str, *, base_url: str | None = None
    ) -> dict[str, Any]:
        """Make *model* on *provider* what new sessions start on, and save it.

        Written to the user's ``config.yaml`` under ``inference:``: provider,
        address and model, never the key. Open sessions are left on whatever
        they are using (DR-036).
        """
        name = self._registered(provider)
        if not (model or "").strip():
            raise BridgeError("which model? a name is required")
        with self._lock:
            config = self._config
        try:
            setup = setup_for_provider(
                config,
                name,
                model.strip(),
                base_url=base_url,
                credentials=self._credentials,
            )
            chosen = setup.inference
            assert chosen is not None
            saved = save_default_inference(chosen.provider, chosen.model, chosen.base_url)
        except ConfigurationError as exc:
            raise BridgeError(str(exc)) from exc
        except OSError as exc:
            raise BridgeError(f"could not save the default: {exc}", status=500) from exc
        with self._lock:
            self._config = self._config.with_inference(
                replace(
                    self._config.inference,
                    provider=chosen.provider,
                    base_url=chosen.base_url,
                    model=chosen.model,
                    api_key=chosen.api_key,
                    context_limit=(
                        self._config.inference.context_limit
                        if chosen.provider == config.inference.provider
                        else None
                    ),
                )
            )
            self._setup = setup
            if not self._open:
                self._problem = ""
                self._open_session(None)
        self._broadcast_state()
        return {**self.providers(), "saved_to": str(saved)}

    def _registered(self, provider: str) -> str:
        name = (provider or "").strip().lower()
        if not name:
            raise BridgeError("which provider? a name is required")
        try:
            return get_provider(name).name
        except ConfigurationError as exc:
            raise BridgeError(str(exc)) from exc

    def _adopt_key(self, provider: str, key: str) -> None:
        """New sessions on *provider* use *key* from now on. Caller holds the lock."""
        if self._config.inference.provider == provider:
            self._config = self._config.with_inference(
                replace(self._config.inference, api_key=key)
            )
        setup = self._setup
        if (
            setup is not None
            and setup.inference is not None
            and setup.inference.provider == provider
        ):
            self._setup = replace(setup, inference=replace(setup.inference, api_key=key))

    def _retry_if_idle(self) -> None:
        """With nothing open (a hosted default that had no key, say), try again."""
        if not self._open:
            self._resolve()

    def _default_payload(self) -> dict[str, Any]:
        setup = self._setup
        inference = (
            setup.inference
            if setup is not None and setup.inference is not None
            else self._config.inference
        )
        return {
            "provider": inference.provider,
            "base_url": inference.base_url,
            "model": inference.model,
        }

    def set_context_window(
        self, tokens: int, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Serve *tokens* from the session's next request on (DR-035)."""
        with self._lock:
            live = self._target(session_id)
            self._require_idle(live)
            try:
                live.session.set_context_window(tokens)
            except AutoSEError as exc:
                raise BridgeError(str(exc)) from exc
        return self._broadcast_state()

    def request_tier(
        self, tier: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Ask for a tier. The session ratchet may return a higher one (DR-003)."""
        with self._lock:
            live = self._target(session_id)
            if tier == "auto":
                live.session.auto_classify = True
                return self._broadcast_state()
            if not is_tier(tier):
                raise BridgeError(f"'{tier}' is not a tier")
            live.session.auto_classify = False
            live.session.request_tier(tier)
        return self._broadcast_state()

    def clear_history(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            live = self._target(session_id)
            self._require_idle(live)
            live.session.clear_history()
        return self._broadcast_state()

    def undo(self, session_id: str | None = None) -> dict[str, Any]:
        """Put the workspace back as it was before this session's last write.

        Refused while *any* session is running, not just this one: an undo
        rewrites files on disk, and the workspace is shared, so putting one
        session's files back while another run is reading and editing them
        would corrupt work nobody asked to lose (DR-024, DR-031).
        """
        with self._lock:
            live = self._target(session_id)
            self._require_idle(live)
            self._require_nothing_running("undo a turn", ignoring=live)
            report = live.session.undo_last_change()
        if report is None:
            raise BridgeError("there is nothing to undo", status=409)
        payload = revert_payload(report)
        payload["state"] = self._broadcast_state()
        return payload

    # -- the turn ----------------------------------------------------------

    def _work(self, live: _Live, prompt: str, tier: str | None) -> None:
        """Run one turn to completion on this thread, narrating as it goes."""
        session = live.session
        try:
            stream = session.stream(prompt, tier=tier)
            for chunk in stream:
                with self._lock:
                    live.answer.append(chunk)
                    self._publish_for(live, {"kind": "answer", "text": chunk})
            turn = stream.turn
            if turn is not None:
                self._publish_for(
                    live,
                    {
                        "kind": "turn",
                        "turn": turn_payload(turn, index=len(session.history) - 1),
                    },
                )
        except UnavailableTierError as exc:
            self._publish_for(
                live,
                {"kind": "failed", "title": "Not implemented yet", "message": str(exc)},
            )
        except AutoSEError as exc:
            self._publish_for(
                live,
                {"kind": "failed", "title": type(exc).__name__, "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - see below
            # A turn that fails in an unforeseen way must reach the browser as
            # a failed card, not vanish with the worker thread and leave the
            # dashboard spinning forever.
            self._publish_for(
                live,
                {
                    "kind": "failed",
                    "title": "Unexpected failure",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            self._deny_pending(live, "the task ended before this call was answered")
            # DR-034/DR-035: how much of the model landed on the GPU at the
            # window it was just loaded with is only knowable afterwards. A
            # refresh is a convenience and must never fail a turn.
            with contextlib.suppress(Exception):
                session.refresh_model_info()
            with self._lock:
                live.running = False
                live.current = None
            self._publish_for(live, {"kind": "busy", "running": False})
            self._broadcast_state()

    def _ask(self, live: _Live, request: ApprovalRequest) -> ApprovalDecision:
        """Put one call to the browser and block until it answers (DR-012).

        Denial is the default in every direction: no answer within the timeout
        is a denial, and a browser that never connected cannot silently let a
        shell command through by being absent.
        """
        if live.session.stop_requested:
            # DR-030: a stop already asked for is an answer to this call — do
            # not put a decision to the browser about work that is ending.
            return ApprovalDecision.deny(
                "the user stopped this run before the call was made"
            )
        pending = _Pending(request)
        with self._lock:
            live.pending[pending.id] = pending
        self._publish_for(live, {"kind": "approval", "request": pending.payload()})
        answered = pending.answered.wait(self._approval_timeout)
        with self._lock:
            live.pending.pop(pending.id, None)
        decision = pending.decision
        if not answered or decision is None:
            decision = ApprovalDecision.deny(
                "nobody answered in the dashboard within "
                f"{int(self._approval_timeout)}s, so the call was refused"
            )
        self._publish_for(
            live,
            {
                "kind": "approval_closed",
                "id": pending.id,
                "approved": decision.approved,
                "reason": decision.reason,
            },
        )
        return decision

    def _deny_pending(self, live: _Live, reason: str) -> None:
        with self._lock:
            waiting = list(live.pending.values())
        for pending in waiting:
            if pending.decision is None:
                pending.decision = ApprovalDecision.deny(reason)
            pending.answered.set()

    # -- plumbing ----------------------------------------------------------

    def subscribe(self, *, since: int = 0) -> _Subscriber:
        return self._bus.subscribe(since=since)

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        self._bus.unsubscribe(subscriber)

    def _on_event(self, live: _Live, event: Event) -> None:
        payload = event_payload(event)
        # Recorded and published under one lock, and the snapshot reads the
        # timeline and the frame sequence under the same lock, so a page that
        # replays the timeline and then streams from that sequence sees every
        # event exactly once (DR-033).
        with self._lock:
            if live.running:
                live.record(payload)
            self._publish_for(live, {"kind": "event", **payload})

    def _publish(self, frame: dict[str, Any]) -> None:
        """A frame about the dashboard rather than about one session."""
        self._bus.publish({"session": "", **frame})

    def _publish_for(self, live: _Live, frame: dict[str, Any]) -> None:
        """A frame the browser must attribute to one card (DR-031)."""
        self._bus.publish({"session": live.id, **frame})

    def _broadcast_state(self) -> dict[str, Any]:
        state = self.snapshot()
        self._publish({"kind": "state", "state": state})
        return state

    def _target(self, session_id: str | None) -> _Live:
        """The session an API call is about: the named one, or the focused one."""
        with self._lock:
            if session_id:
                live = self._open.get(session_id)
                if live is None:
                    raise BridgeError(
                        f"session '{session_id}' is not open", status=404
                    )
                return live
            live = self._open.get(self._focus)
        if live is None:
            raise BridgeError(self._problem or "no session is open", status=503)
        return live

    def _require_idle(self, live: _Live) -> None:
        if live.running:
            raise BridgeError(
                "wait for this session's task to finish first", status=409
            )

    def _require_desk_room(self) -> None:
        if len(self._open) >= MAX_OPEN_SESSIONS:
            raise BridgeError(
                f"{MAX_OPEN_SESSIONS} sessions are already open — close one first",
                status=409,
            )

    def _require_nothing_running(
        self, action: str, *, ignoring: _Live | None = None
    ) -> None:
        """Refuse an action that cannot share the workspace with a live run."""
        busy = [
            one.id
            for one in self._open.values()
            if one.running and one is not ignoring
        ]
        if busy:
            names = ", ".join(identifier[:8] for identifier in busy)
            raise BridgeError(
                f"cannot {action} while {len(busy)} other session(s) are working "
                f"({names}) — stop them or wait",
                status=409,
            )


def _apply_overrides(fresh: AutoSEConfig, previous: AutoSEConfig) -> AutoSEConfig:
    """Carry command-line choices across a config reload.

    ``--yes`` was a decision about *this run*, made by a person who is still
    running it; re-reading the config file must not quietly turn it off (nor,
    since it is attributed, turn it on).
    """
    if not previous.auto_approve:
        return fresh
    return replace(
        fresh,
        auto_approve=True,
        auto_approve_authorized_by=previous.auto_approve_authorized_by,
    )

def _context_payload(session: Session) -> dict[str, Any] | None:
    """DR-034's verdict plus, where AutoSE chooses the window, DR-035's choices."""
    info = session.model_info
    if info is None:
        return None
    adjustable = serves_requested_window(session.inference.provider)
    payload = advise_context(
        info,
        provider=session.inference.provider,
        min_usable=MIN_USABLE_CONTEXT_TOKENS,
        adjustable=adjustable,
    ).to_dict()
    payload["choices"] = (
        [
            choice.to_dict()
            for choice in window_choices(info, min_usable=MIN_USABLE_CONTEXT_TOKENS)
        ]
        if adjustable
        else []
    )
    payload["gpu_fraction"] = info.gpu_fraction
    return payload
