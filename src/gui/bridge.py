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
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from logic import __version__
from logic.automation import (
    AutomationError,
    AutomationStore,
    CommandRunner,
    TrustFile,
    UnknownAutomationError,
    run_watch_command,
)
from logic.common.allowances import Allowance, allowance_for_rule, offer_for
from logic.common.browse import list_workspace, read_workspace_file
from logic.common.workspace import Workspace
from logic.errors import WorkspaceEscapeError
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
from logic.user_config import save_default_inference, user_config_dir
from logic.workflows import UnavailableTierError
from logic.workflows.review import PlanDecision, PlanReview

from .automation import TICK_SECONDS, AutomationRunner, SkippedRun
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

#: Where the commands a person saved as watchers are vouched for (DR-047):
#: outside every workspace, so a repository cannot vouch for its own.
TRUSTED_COMMANDS_FILENAME = "trusted-watchers.json"

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

#: How far back a session strip's timeline reaches.
LANE_SECONDS = 30 * 60

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
        offer = offer_for(self.request)
        return {
            "id": self.id,
            "created_at": self.created_at,
            **self.request.to_dict(),
            # DR-048: what "allow for this session" would cover, or why this
            # call can only be answered once.
            "allow": offer.allowance.to_dict() if offer.allowance else None,
            "allow_note": offer.reason,
        }


class _PlanPending:
    """A plan parked for the person to review (DR-045)."""

    def __init__(self, review: PlanReview) -> None:
        self.id = secrets.token_hex(8)
        self.review = review
        self.answered = threading.Event()
        self.decision: PlanDecision | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan": self.review.plan,
            "requirements": [dict(item) for item in self.review.requirements],
            "pass_number": self.review.pass_number,
            "problem": self.review.problem,
        }


class _BridgePlanGate:
    """The dashboard's ``PlanReviewGate``: parks the plan until answered."""

    def __init__(self, ask: Callable[[PlanReview], PlanDecision]) -> None:
        self._ask = ask

    def review(self, review: PlanReview) -> PlanDecision:
        return self._ask(review)


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
        #: The session's state over time, for its strip (see ``mark``).
        self.lane: list[tuple[float, str]] = [(time.time(), "idle")]
        self.answer: list[str] = []
        self.pending: dict[str, _Pending] = {}
        self.opened_at = time.time()
        # DR-033: what the current turn has done so far, for a page that opens
        # after it started. Reset by every new turn.
        self.timeline: list[dict[str, Any]] = []
        # What the session's strip shows (see `_note_strip`), per turn.
        self.stage_names: list[str] = []
        self.stage: dict[str, Any] | None = None
        self.requirement_counts: dict[str, int] | None = None
        self.context_pct: int | None = None
        self.narration = ""
        self.thinking = ""
        # DR-042: prompts waiting for this session's running turn to end, in
        # order. Held — not started — after a turn the user stopped or that
        # failed, so a stop means "wait", not "skip to the next one".
        self.queue: list[dict[str, Any]] = []
        self.queue_held = False
        # DR-044, DR-048: what the person said may run unasked for the rest of
        # this session, by key: a command's leading words, a host, or a tool.
        # Per session, never global, and gone when the session closes.
        self.allowances: dict[str, Allowance] = {}
        # DR-045: whether staged turns stop for a plan review, and the plan
        # waiting for one.
        self.review_plans = False
        self.plan_pending: _PlanPending | None = None
        #: The models of the provider this session switched to (DR-036), for
        #: the picker; ``None`` while it is still on the default provider.
        self.models: tuple[ModelInfo, ...] | None = None
        #: DR-047: opened by a schedule or a watcher, with nobody watching.
        #: Its approvals are refused at once rather than parked for a person.
        self.unattended = False
        #: The schedule or watcher that opened it, and the name it shows.
        self.origin: dict[str, Any] | None = None
        self.title = ""

    def queue_payload(self) -> dict[str, Any]:
        return {"queue": [dict(item) for item in self.queue], "held": self.queue_held}

    def reset_turn(self) -> None:
        self.answer = []
        self.timeline = []
        self.narration = ""
        self.thinking = ""
        self.stage_names = []
        self.stage = None
        self.requirement_counts = None
        self.context_pct = None

    def mark(self, status: str, at: float | None = None) -> None:
        """Note what the session is doing now, for its strip's timeline.

        One of ``working``, ``waiting`` (on the person), ``idle`` or
        ``failed``; only changes are kept, and only the last
        :data:`LANE_SECONDS` of them, plus the one in force at its start.
        """
        now = time.time() if at is None else at
        if self.lane and self.lane[-1][1] == status:
            return
        self.lane.append((now, status))
        cutoff = now - LANE_SECONDS
        while len(self.lane) > 1 and self.lane[1][0] <= cutoff:
            del self.lane[0]

    def strip_payload(self) -> dict[str, Any]:
        """What the session's strip in the rail shows besides its title."""
        return {
            "stage": dict(self.stage) if self.stage else None,
            "requirements": dict(self.requirement_counts) if self.requirement_counts else None,
            "context_pct": self.context_pct,
            "lane": [[round(at, 1), status] for at, status in self.lane],
        }

    def _note_strip(self, kind: Any, data: dict[str, Any]) -> None:
        if kind == "workflow_started":
            self.stage_names = [str(name) for name in data.get("stages") or ()]
        elif kind == "stage_started":
            name = str(data.get("stage") or "")
            index = self.stage_names.index(name) if name in self.stage_names else -1
            self.stage = {
                "name": name,
                "title": str(data.get("title") or name),
                "number": (index + 1) * 10 if index >= 0 else None,
            }
        elif kind == "stage_finished" and data.get("requirements"):
            statuses = [str(item.get("status", "")) for item in data["requirements"]]
            self.requirement_counts = {
                "total": len(statuses),
                "verified": statuses.count("Verified"),
                "failed": statuses.count("Failed"),
            }
        elif kind == "round_usage":
            limit = data.get("context_limit")
            used = data.get("prompt_tokens") or data.get("estimated_input_tokens") or 0
            if isinstance(limit, int) and limit > 0 and used:
                self.context_pct = min(100, round(100 * used / limit))

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
        self._note_strip(kind, payload.get("data") or {})
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
        clock: Callable[[], float] = time.time,
        automation_interval: float = TICK_SECONDS,
        command_runner: CommandRunner = run_watch_command,
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
        # DR-047: schedules and watchers for this workspace, and the runner
        # that acts on them. The runner is built here but only ticks once
        # `start_automation` is called — a bridge in a test never spawns it.
        self._automation = self._automation_store()
        self._runner = AutomationRunner(
            self,
            lambda: self._automation,
            clock=clock,
            interval=automation_interval,
            command_runner=command_runner,
        )
        self._clock = clock
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

    def _open_session(
        self, record: SessionRecord | None, *, focus: bool = True
    ) -> _Live | None:
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
        if focus or not self._focus:
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
            # The workspace may have changed, and its automation with it.
            self._automation = self._automation_store()
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

    def rename_session(self, session_id: str, name: str) -> dict[str, Any]:
        """Give a session a name, open or stored; an empty name clears it."""
        name = " ".join(str(name).split())[:80]
        with self._lock:
            live = self._open.get(session_id)
            if live is None:
                record = self._store.load(session_id)
                if record is None:
                    raise BridgeError(f"no session '{session_id}'", status=404)
                self._store.save(replace(record, name=name))
        if live is not None:
            live.session.rename(name)
            self._broadcast_state()
        return {"renamed": session_id, "name": name, "sessions": self.sessions()}

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
        self._runner.stop()
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
            # DR-047: enough for the header to say what runs on its own.
            "automation": {
                **self._automation.summary(),
                "running": self._runner.alive,
            },
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
                    "queue": [],
                    "queue_held": False,
                    "allowances": [],
                    "review_plans": False,
                    "pending_plan": None,
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
            queued = live.queue_payload()
            allowed = _allowances_payload(live)
            plan_waiting = live.plan_pending.payload() if live.plan_pending else None
            review_plans = live.review_plans
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
                # DR-042: what waits for this session's running turn.
                "queue": queued["queue"],
                "queue_held": queued["held"],
                "allowances": allowed,
                "review_plans": review_plans,
                "pending_plan": plan_waiting,
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
            "title": session.name or live.title or session_title(session.history, live.current),
            "named": bool(session.name),
            "automation": dict(live.origin) if live.origin else None,
            "unattended": live.unattended,
            "tier": session.tier,
            "auto_classify": session.auto_classify,
            "running": live.running,
            "stopping": bool(live.running and session.stop_requested),
            "current": dict(live.current) if live.current else None,
            "waiting": len(live.pending),
            "queued": len(live.queue),
            "plan_waiting": live.plan_pending is not None,
            "queue_held": live.queue_held,
            "turns": len(session.history),
            "total_tokens": session.usage.total_tokens,
            "opened_at": live.opened_at,
            "focused": live.id == self._focus,
            "strip": live.strip_payload(),
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

    def changes(
        self, session_id: str | None = None, *, turn: int | None = None
    ) -> dict[str, Any]:
        """One turn's writes, with diffs — the last turn's by default (DR-024).

        *turn* is an index into the session's history, so the Changes tab can
        read any turn's diffs on demand rather than receiving every diff of
        the session with each state frame (DR-040).
        """
        session = self._target(session_id).session
        if turn is None:
            changes = session.last_changes
        else:
            history = session.history
            if not 0 <= turn < len(history):
                raise BridgeError(f"there is no run {turn} in this session", status=404)
            changes = history[turn].changes
        added = sum(change.stats[0] for change in changes)
        removed = sum(change.stats[1] for change in changes)
        return {
            "session": session.session_id,
            "turn": turn,
            "changes": [
                change_payload(change, with_diff=True) for change in changes
            ],
            "added": added,
            "removed": removed,
        }

    def files(self) -> dict[str, Any]:
        """Every file in the workspace, for the Files view (DR-051)."""
        workspace = Workspace.at(self._config.resolve_workspace_root())
        files, truncated = list_workspace(workspace)
        return {"root": str(workspace.root), "files": files, "truncated": truncated}

    def file_text(self, path: str) -> dict[str, Any]:
        """One workspace file's text, read-only (DR-051)."""
        if not path:
            raise BridgeError("which file? a path is required")
        workspace = Workspace.at(self._config.resolve_workspace_root())
        try:
            return read_workspace_file(workspace, path).to_dict()
        except WorkspaceEscapeError as error:
            raise BridgeError(str(error), status=403) from error
        except PermissionError as error:
            raise BridgeError(str(error), status=403) from error
        except FileNotFoundError as error:
            raise BridgeError(str(error), status=404) from error
        except OSError as error:
            raise BridgeError(f"{path} could not be read: {error.strerror or error}") from error

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
        queue: bool = False,
        unattended: bool = False,
    ) -> dict[str, Any]:
        """Start a turn on a worker thread. Returns as soon as it is running.

        With *queue*, a prompt for a session that is already working waits in
        that session's queue and starts when the running turn ends (DR-042).

        *unattended* is for the automation runner only (DR-047). A prompt a
        person sends — even into a session a schedule opened — is attended,
        so its approvals go to the browser as usual.
        """
        text = prompt.strip()
        if not text:
            raise BridgeError("there is nothing to run — describe a request first")
        if tier and not is_tier(tier):
            raise BridgeError(f"'{tier}' is not a tier")
        with self._lock:
            live = self._target(session_id)
            session = live.session
            if live.running and queue:
                item = {
                    "id": secrets.token_hex(6),
                    "prompt": text,
                    "tier": tier or "",
                    "queued_at": time.time(),
                }
                live.queue.append(item)
                self._publish_for(live, {"kind": "queue", **live.queue_payload()})
                return {"queued": True, "id": item["id"], "session": live.id}
            if live.running:
                raise BridgeError(
                    "a run is already working in this session; queue this one to "
                    "start when it ends",
                    status=409,
                )
            live.running = True
            live.unattended = unattended
            live.reset_turn()
            live.mark("working")
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
        self,
        approval_id: str,
        *,
        approved: bool,
        reason: str = "",
        scope: str = "once",
    ) -> dict[str, Any]:
        """Answer one parked approval-required call (DR-012).

        Found by id across every open session, because the browser answers the
        call it was shown and should not have to say which card it came from.
        """
        if scope not in ("once", "session"):
            raise BridgeError(f"'{scope}' is not once or session")
        with self._lock:
            pending = None
            holder = None
            for live in self._open.values():
                pending = live.pending.get(approval_id)
                if pending is not None:
                    holder = live
                    break
            standing: Allowance | None = None
            if pending is not None and holder is not None and approved and scope == "session":
                # DR-048: the answer covers this call and every later one like
                # it: the same command words, the same host, or the same tool.
                offer = offer_for(pending.request)
                if offer.allowance is None:
                    raise BridgeError(f"this call can only be allowed once: {offer.reason}")
                standing = offer.allowance
                holder.allowances[standing.key] = standing
        if pending is None:
            # Already resolved, or timed out while the browser was deciding.
            raise BridgeError(
                "that approval is no longer waiting for an answer", status=409
            )
        pending.decision = (
            ApprovalDecision.allow(
                reason.strip()
                or (
                    f"the user allowed {standing.label} for the rest of this session"
                    if standing is not None
                    else "the user approved this call"
                )
            )
            if approved
            else ApprovalDecision.deny(
                reason.strip() or "the user declined this call in the dashboard"
            )
        )
        pending.answered.set()
        if holder is not None and standing is not None:
            # Calls already waiting that the new allowance covers are answered
            # by it too, as they would have been had they arrived a moment later.
            with self._lock:
                covered = [
                    other
                    for other in holder.pending.values()
                    if other is not pending
                    and other.decision is None
                    and standing.covers(other.request)
                ]
            for other in covered:
                other.decision = ApprovalDecision.allow(
                    f"{standing.label} were allowed for the rest of this session"
                )
                other.answered.set()
            self._publish_for(
                holder, {"kind": "allowances", "allowances": _allowances_payload(holder)}
            )
        return {"resolved": approval_id, "approved": approved, "scope": scope}

    def set_plan_review(
        self, enabled: bool, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Turn plan review on or off for a session's next turns (DR-045)."""
        with self._lock:
            live = self._target(session_id)
            live.review_plans = enabled
            live.session.review_plans_with(
                _BridgePlanGate(partial(self._review_plan, live)) if enabled else None
            )
        return self._broadcast_state()

    def resolve_plan(
        self,
        plan_id: str,
        *,
        action: str,
        feedback: str = "",
        plan: str = "",
    ) -> dict[str, Any]:
        """Answer a parked plan review (DR-045)."""
        if action not in ("approve", "revise", "edit", "stop"):
            raise BridgeError(f"'{action}' is not approve, revise, edit or stop")
        if action == "revise" and not feedback.strip():
            raise BridgeError("say what to change in the plan")
        if action == "edit" and not plan.strip():
            raise BridgeError("the edited plan is empty")
        with self._lock:
            pending = next(
                (
                    live.plan_pending
                    for live in self._open.values()
                    if live.plan_pending is not None and live.plan_pending.id == plan_id
                ),
                None,
            )
        if pending is None:
            raise BridgeError("that plan is no longer waiting for a review", status=409)
        pending.decision = PlanDecision(
            action=action,  # type: ignore[arg-type]
            feedback=feedback.strip(),
            plan=plan,
        )
        pending.answered.set()
        return {"resolved": plan_id, "action": action}

    def _review_plan(self, live: _Live, review: PlanReview) -> PlanDecision:
        """Park the plan and wait for the person, or for a stop (DR-045).

        No timeout, unlike an approval: nothing has been changed yet, so a plan
        left unanswered costs nothing but waiting, and a stop ends the wait.
        """
        pending = _PlanPending(review)
        with self._lock:
            live.plan_pending = pending
            live.mark("waiting")
            self._publish_for(live, {"kind": "plan_review", "review": pending.payload()})
        while not pending.answered.wait(1.0):
            if live.session.stop_requested:
                pending.decision = PlanDecision.stop("the run was stopped at plan review")
                break
        decision = pending.decision or PlanDecision.stop("the plan review ended unanswered")
        with self._lock:
            if live.plan_pending is pending:
                live.plan_pending = None
                if not live.pending:
                    live.mark("working")
            self._publish_for(
                live, {"kind": "plan_review_closed", "id": pending.id, "action": decision.action}
            )
        return decision

    def add_allowance(
        self, tool: str, pattern: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Add a standing yes by hand, held to DR-048's rules (DR-050).

        Calls already waiting that the new rule covers are answered by it, as
        when the same yes is given from a card.
        """
        try:
            standing = allowance_for_rule(tool, pattern)
        except ValueError as error:
            raise BridgeError(str(error)) from error
        with self._lock:
            live = self._target(session_id)
            live.allowances[standing.key] = standing
            covered = [
                pending
                for pending in live.pending.values()
                if pending.decision is None and standing.covers(pending.request)
            ]
            allowances = _allowances_payload(live)
        for pending in covered:
            pending.decision = ApprovalDecision.allow(
                f"{standing.label} were allowed for the rest of this session"
            )
            pending.answered.set()
        self._publish_for(live, {"kind": "allowances", "allowances": allowances})
        return {"session": live.id, "allowances": allowances, "added": standing.to_dict()}

    def revoke_allowance(
        self, allowance_id: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        """Take back an "allow for this session" (DR-044, DR-048)."""
        with self._lock:
            live = self._target(session_id)
            if live.allowances.pop(allowance_id, None) is None:
                raise BridgeError(
                    f"'{allowance_id}' is not allowed in this session", status=404
                )
            allowances = _allowances_payload(live)
        self._publish_for(live, {"kind": "allowances", "allowances": allowances})
        return {"session": live.id, "allowances": allowances}

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
            setup = self._setup
            if (
                provider
                and not base_url
                and setup is not None
                and setup.inference is not None
                and provider.strip().lower() == setup.inference.provider.lower()
            ):
                # Back to the default provider means back to the default's
                # address too: the one this dashboard was started against (an
                # SSH tunnel on another port, say), not the provider's usual one.
                base_url = setup.inference.base_url
            try:
                session.select_model(name, provider=provider, base_url=base_url)
            except AutoSEError as exc:
                raise BridgeError(str(exc)) from exc
            after = (session.inference.provider, session.inference.base_url)
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
        """Use the tier the person picked, up or down (DR-049)."""
        with self._lock:
            live = self._target(session_id)
            if tier == "auto":
                live.session.auto_classify = True
                return self._broadcast_state()
            if not is_tier(tier):
                raise BridgeError(f"'{tier}' is not a tier")
            live.session.auto_classify = False
            live.session.choose_tier(tier)
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

    # -- automation (DR-047) ------------------------------------------------

    def _automation_store(self) -> AutomationStore:
        root = self._config.resolve_workspace_root()
        return AutomationStore(
            root, trust=TrustFile(user_config_dir() / TRUSTED_COMMANDS_FILENAME, root)
        )

    def start_automation(self) -> None:
        """Start ticking. Called by the server once it is serving."""
        self._runner.start()

    def automation(self) -> dict[str, Any]:
        """Every schedule and watcher, and whether anything is acting on them."""
        return {
            **self._automation.payload(),
            "running": self._runner.alive,
            # Whether a scheduled run's shell and network calls are refused or
            # approved: only `--yes`, attributed, turns refusal off.
            "unattended_approvals": "auto" if self._config.auto_approve else "deny",
            "authorized_by": (
                self._config.auto_approve_authorized_by
                if self._config.auto_approve
                else ""
            ),
            "file": str(self._automation.path),
        }

    def automation_action(self, action: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Add, change, delete or run a schedule or watcher, by *action* name.

        Validation failures are a 400 with the store's own sentence; an id
        that names nothing is a 404.
        """
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "add_schedule": self._add_schedule,
            "update_schedule": self._update_schedule,
            "delete_schedule": self._delete_schedule,
            "run_now": self._run_now,
            "add_watcher": self._add_watcher,
            "update_watcher": self._update_watcher,
            "delete_watcher": self._delete_watcher,
            "check_now": self._check_now,
        }
        handler = handlers.get(action)
        if handler is None:
            raise BridgeError(f"'{action}' is not one of {', '.join(handlers)}")
        try:
            result = handler(fields)
        except UnknownAutomationError as exc:
            raise BridgeError(str(exc), status=404) from exc
        except AutomationError as exc:
            raise BridgeError(str(exc)) from exc
        except SkippedRun as exc:
            raise BridgeError(f"could not start it now: {exc}", status=409) from exc
        self._broadcast_state()
        return {**result, "automation": self.automation()}

    def _add_schedule(self, fields: dict[str, Any]) -> dict[str, Any]:
        schedule = self._automation.add_schedule(fields, now=self._clock())
        return {"schedule": schedule.to_dict()}

    def _update_schedule(self, fields: dict[str, Any]) -> dict[str, Any]:
        schedule = self._automation.update_schedule(
            _id_of(fields), fields, now=self._clock()
        )
        return {"schedule": schedule.to_dict()}

    def _delete_schedule(self, fields: dict[str, Any]) -> dict[str, Any]:
        identifier = _id_of(fields)
        self._automation.delete_schedule(identifier)
        return {"deleted": identifier}

    def _run_now(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._runner.run_schedule_now(_id_of(fields))

    def _add_watcher(self, fields: dict[str, Any]) -> dict[str, Any]:
        watcher = self._automation.add_watcher(fields, now=self._clock())
        return {"watcher": watcher.to_dict(trusted=True)}

    def _update_watcher(self, fields: dict[str, Any]) -> dict[str, Any]:
        watcher = self._automation.update_watcher(
            _id_of(fields), fields, now=self._clock()
        )
        return {"watcher": watcher.to_dict(trusted=self._automation.trusted(watcher))}

    def _delete_watcher(self, fields: dict[str, Any]) -> dict[str, Any]:
        identifier = _id_of(fields)
        self._automation.delete_watcher(identifier)
        return {"deleted": identifier}

    def _check_now(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._runner.check_watcher_now(_id_of(fields))

    # The runner's view of the desk (gui.automation.Desk).

    def start_unattended(
        self, *, title: str, prompt: str, tier: str | None, origin: dict[str, Any]
    ) -> str:
        """Open a session for a schedule or watcher and start *prompt* in it.

        Never takes the focus from whatever the person is looking at. When the
        desk is full, finished unattended sessions that nobody is looking at
        are closed first — they are on disk and reopen from the rail — but a
        session the person opened is never closed to make room; then the run
        is skipped and retried on the next tick.
        """
        with self._lock:
            if self._setup is None or not self._setup.is_ready:
                raise SkippedRun(self._problem or "there is no model to run it on")
            self._make_room_for_unattended()
            if len(self._open) >= MAX_OPEN_SESSIONS:
                raise SkippedRun(
                    f"desk full: {MAX_OPEN_SESSIONS} sessions are open; close one "
                    "and it runs on the next tick"
                )
            live = self._open_session(None, focus=False)
            if live is None:
                raise SkippedRun(self._problem or "could not open a session")
            live.unattended = True
            live.origin = dict(origin)
            live.title = title
        try:
            self.submit(prompt, tier=tier, session_id=live.id, unattended=True)
        except BridgeError as exc:
            raise SkippedRun(str(exc)) from exc
        return live.id

    def _make_room_for_unattended(self) -> None:
        """Close idle unattended sessions off-screen, oldest first. Lock held."""
        idle = [
            one
            for one in self._open.values()
            if one.unattended
            and not one.running
            and not one.pending
            and not one.queue
            and one.id != self._focus
        ]
        while len(self._open) >= MAX_OPEN_SESSIONS and idle:
            del self._open[idle.pop(0).id]

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            live = self._open.get(session_id)
            return live is not None and live.running

    def publish_automation(self, frame: dict[str, Any]) -> None:
        self._publish(frame)
        self._broadcast_state()

    # -- the turn ----------------------------------------------------------

    def _work(self, live: _Live, prompt: str, tier: str | None) -> None:
        """Run one turn to completion on this thread, narrating as it goes."""
        session = live.session
        # Whether the queue may carry on afterwards: only after a turn that
        # ran to its end (DR-042).
        ended_cleanly = False
        try:
            stream = session.stream(prompt, tier=tier)
            for chunk in stream:
                with self._lock:
                    live.answer.append(chunk)
                    self._publish_for(live, {"kind": "answer", "text": chunk})
            turn = stream.turn
            ended_cleanly = turn is not None and turn.stop_reason != "cancelled"
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
            self._deny_pending(live, "the run ended before this call was answered")
            # DR-034/DR-035: how much of the model landed on the GPU at the
            # window it was just loaded with is only knowable afterwards. A
            # refresh is a convenience and must never fail a turn.
            with contextlib.suppress(Exception):
                session.refresh_model_info()
            with self._lock:
                live.running = False
                live.current = None
                last = session.history[-1] if session.history else None
                live.mark(
                    "failed"
                    if last is not None and not last.succeeded and last.stop_reason != "cancelled"
                    else "idle"
                )
                following = None
                if live.queue:
                    if ended_cleanly and not live.queue_held:
                        following = live.queue.pop(0)
                    else:
                        live.queue_held = True
                    self._publish_for(live, {"kind": "queue", **live.queue_payload()})
            self._publish_for(live, {"kind": "busy", "running": False})
            self._broadcast_state()
            if following is not None:
                self._start_queued(live, following)

    def _start_queued(self, live: _Live, item: dict[str, Any]) -> None:
        """Run the next queued prompt; a refusal holds the queue (DR-042)."""
        try:
            self.submit(item["prompt"], tier=item["tier"] or None, session_id=live.id)
        except BridgeError as exc:
            with self._lock:
                live.queue.insert(0, item)
                live.queue_held = True
                self._publish_for(live, {"kind": "queue", **live.queue_payload()})
            self._publish_for(
                live,
                {"kind": "failed", "title": "Queue held", "message": str(exc)},
            )

    def queue_action(
        self,
        action: str,
        *,
        session_id: str | None = None,
        item_id: str | None = None,
        to: int | None = None,
    ) -> dict[str, Any]:
        """Change a session's queue: remove or move an item, clear, or resume."""
        following = None
        with self._lock:
            live = self._target(session_id)
            if action == "remove":
                before = len(live.queue)
                live.queue = [item for item in live.queue if item["id"] != item_id]
                if len(live.queue) == before:
                    raise BridgeError(f"nothing queued with id '{item_id}'", status=404)
            elif action == "move":
                index = next(
                    (i for i, item in enumerate(live.queue) if item["id"] == item_id), None
                )
                if index is None:
                    raise BridgeError(f"nothing queued with id '{item_id}'", status=404)
                if to is None:
                    raise BridgeError("move needs a position")
                item = live.queue.pop(index)
                live.queue.insert(max(0, min(to, len(live.queue))), item)
            elif action == "clear":
                live.queue = []
            elif action == "resume":
                live.queue_held = False
                if live.queue and not live.running:
                    following = live.queue.pop(0)
            else:
                raise BridgeError(f"'{action}' is not remove, move, clear or resume")
            if not live.queue:
                live.queue_held = False
            payload = live.queue_payload()
            self._publish_for(live, {"kind": "queue", **payload})
        if following is not None:
            self._start_queued(live, following)
        return {"session": live.id, **payload}

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
        with self._lock:
            standing = next(
                (item for item in live.allowances.values() if item.covers(request)), None
            )
        if standing is not None:
            # DR-044, DR-048: the person already said yes to calls like this
            # for the rest of the session. The executor still records the
            # request and the answer as events, so the call is never invisible.
            return ApprovalDecision.allow(
                f"{standing.label} were allowed for the rest of this session"
            )
        if live.unattended:
            # DR-047: a scheduled run is unattended by definition. Parking the
            # call for fifteen minutes would only hold the run hostage to a
            # person who is not there; refuse it now and say how to get it.
            # (With `--yes` this gate is never consulted at all.)
            return ApprovalDecision.deny(
                f"unattended run: {request.tool_name} needs a person; run it "
                "from the dashboard to approve it"
            )
        pending = _Pending(request)
        with self._lock:
            live.pending[pending.id] = pending
            live.mark("waiting")
        self._publish_for(live, {"kind": "approval", "request": pending.payload()})
        answered = pending.answered.wait(self._approval_timeout)
        with self._lock:
            live.pending.pop(pending.id, None)
            if not live.pending and live.plan_pending is None:
                live.mark("working")
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
            plan = live.plan_pending
        if plan is not None and plan.decision is None:
            plan.decision = PlanDecision.stop(reason)
            plan.answered.set()
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
                "wait for this session's run to finish first", status=409
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


def _allowances_payload(live: _Live) -> list[dict[str, str]]:
    return [item.to_dict() for item in sorted(live.allowances.values(), key=lambda a: a.key)]


def _id_of(fields: dict[str, Any]) -> str:
    identifier = str(fields.get("id") or "").strip()
    if not identifier:
        raise BridgeError("which one? an id is required")
    return identifier
