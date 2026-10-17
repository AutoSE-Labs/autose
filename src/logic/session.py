"""The public entry point every Adapter drives (DR-002).

A ``Session`` is what ``src/tui/`` and ``src/gui/`` talk to, and the only thing
they talk to. It owns the parts of a run that are computation rather than
presentation: which runtime and model to use, which tier a prompt gets and how
that ratchets, which workflow implements that tier, and what bounded history
carries between prompts.

An Adapter supplies three things and renders the rest: an :class:`EventSink`
to receive what happens, an :class:`ApprovalGate` to answer DR-012's
questions, and prompts. It never constructs an agent, a tool registry, or a
chat model itself — those are all behind this surface, which is what keeps two
interfaces behaving identically.
"""

from __future__ import annotations

import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass, field, replace

from .classifier import Classification, TaskClassifier
from .common.approval import (
    ApprovalGate,
    ApprovalPolicy,
    AutoApproveGate,
    DenyingApprovalGate,
)
from .common.cancel import CancelToken
from .common.changes import ChangeJournal, FileChange, RevertReport
from .common.context import MIN_USABLE_CONTEXT_TOKENS
from .common.events import Event, EventSink, NullEventSink
from .common.memory import SessionMemory
from .common.workspace import Workspace
from .config import AutoSEConfig
from .errors import ConfigurationError
from .inference.base import ChatModel
from .inference.catalog import ModelInfo
from .inference.discovery import DetectedRuntime, discover_local_runtimes
from .inference.ollama import OLLAMA_CONTEXT_ENV
from .inference.registry import create_chat_model, create_model_catalog, local_providers
from .inference.settings import InferenceSettings
from .inference.types import Usage
from .limits import count_limit
from .persistence import (
    NullSessionStore,
    SessionRecord,
    SessionStore,
    TurnRecord,
    new_session_id,
)
from .prompt_overrides import PromptOverrides
from .tiers import SPARK, TIER_SPECS, normalise_tier, ratchet
from .tuning import Tuning
from .workflows import (
    UnavailableTierError,
    WorkflowOutcome,
    WorkflowRequest,
    available_tiers,
    create_workflow,
    supports_tier,
)

__all__ = ["Session", "Turn", "SessionSetup", "setup_from_config"]

#: Tokens allowed for one rolling-summary compression call. Small on purpose:
#: this runs when history overflows, and it is condensing, not writing.
_SUMMARY_MAX_TOKENS = 400

_SUMMARY_PROMPT = """\
Condense this conversation history into at most 8 short bullet points capturing
what was asked, what was decided, and what changed. Keep file paths and names
exact. Write only the bullets.

{history}
"""


@dataclass(frozen=True)
class Turn:
    """One completed prompt/answer exchange."""

    prompt: str
    answer: str
    tier: str
    stop_reason: str = "completed"
    usage: Usage = field(default_factory=Usage)
    #: How the tier was chosen, when it was classified rather than requested.
    classification: Classification | None = None
    artifacts: Mapping[str, str] = field(default_factory=dict)
    files_changed: tuple[str, ...] = ()
    #: Every file this turn wrote, with the content either side of the change
    #: (DR-024). Carrying both halves on the turn is what makes the change
    #: reviewable as a diff and revertible after the run has finished.
    changes: tuple[FileChange, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "completed"

    @property
    def changed_anything(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True)
class SessionSetup:
    """The result of working out what to run against, before a session starts.

    Separated from :class:`Session` so an Adapter can *show* the outcome of
    discovery — which runtimes answered, which models each serves, what needs
    choosing — and let the user adjust it before anything is committed to.
    """

    config: AutoSEConfig
    runtimes: tuple[DetectedRuntime, ...] = ()
    inference: InferenceSettings | None = None
    model_info: ModelInfo | None = None
    #: Non-fatal things the user should know: no runtime found, a chosen model
    #: that cannot tool-call, an endpoint that answered but serves nothing.
    warnings: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.inference is not None and self.inference.is_ready

    @property
    def available_models(self) -> tuple[ModelInfo, ...]:
        if self.inference is None:
            return ()
        runtime = self.runtime_for(self.inference.base_url)
        return runtime.models if runtime else ()

    def runtime_for(self, base_url: str) -> DetectedRuntime | None:
        return next(
            (runtime for runtime in self.runtimes if runtime.base_url == base_url), None
        )

    def with_model(self, name: str) -> SessionSetup:
        """Select a different model, carrying its published context limit."""
        if self.inference is None:
            raise ConfigurationError("no inference endpoint has been resolved yet")
        info = next(
            (model for model in self.available_models if model.name == name), None
        )
        inference = self.inference.with_model(name)
        if info is not None and info.context_length:
            inference = inference.with_context_limit(info.context_length)
        return replace(
            self,
            inference=inference,
            model_info=info,
            warnings=_capability_warnings(info),
        )


def setup_from_config(config: AutoSEConfig, *, discover: bool = True) -> SessionSetup:
    """Resolve what to run against: explicit configuration first, then discovery.

    DR-015: a user who has already configured a model is never probed, and a
    user who has not is never asked for values AutoSE can find out.
    """
    configured = config.inference

    if configured.is_ready:
        # Fully configured. Enumerate anyway when we cheaply can, so the model
        # picker and the real context limit are still available — but never
        # let a failed enumeration block a run that was explicitly set up.
        runtime = _enumerate(configured)
        info = runtime.model(configured.model) if runtime else None
        inference = configured
        if (
            info is not None
            and info.context_length
            and configured.context_limit is None
        ):
            inference = configured.with_context_limit(info.context_length)
        return SessionSetup(
            config=config,
            runtimes=(runtime,) if runtime else (),
            inference=inference,
            model_info=info,
            warnings=_capability_warnings(info),
        )

    if not discover:
        return SessionSetup(
            config=config,
            inference=configured,
            warnings=("no model selected, and discovery was disabled",),
        )

    runtimes = discover_local_runtimes(timeout=configured.discovery_timeout)
    usable = next((runtime for runtime in runtimes if runtime.usable), None)
    if usable is None:
        return SessionSetup(
            config=config,
            runtimes=runtimes,
            inference=configured,
            warnings=(_no_runtime_message(runtimes),),
        )

    inference = replace(
        usable.settings(),
        api_key=configured.api_key,
        temperature=configured.temperature,
        timeout=configured.timeout,
        discovery_timeout=configured.discovery_timeout,
    )
    if configured.context_limit:
        inference = inference.with_context_limit(configured.context_limit)
    info = usable.model(inference.model)
    return SessionSetup(
        config=config,
        runtimes=runtimes,
        inference=inference,
        model_info=info,
        warnings=_capability_warnings(info),
    )


class Session:
    """One interactive session: bounded history, a tier ratchet, and dispatch."""

    def __init__(
        self,
        *,
        inference: InferenceSettings,
        workspace: Workspace,
        events: EventSink | None = None,
        approval_gate: ApprovalGate | None = None,
        model_info: ModelInfo | None = None,
        default_tier: str = SPARK,
        auto_classify: bool = True,
        tuning: Tuning | None = None,
        prompt_overrides: PromptOverrides | None = None,
        require_edit_approval: bool = False,
        store: SessionStore | None = None,
        session_id: str = "",
    ) -> None:
        self._inference = inference.require_model()
        self._workspace = workspace
        self._events = events or NullEventSink()
        # DR-012: no gate means refuse, never means run unreviewed.
        self._approval_gate = approval_gate or DenyingApprovalGate()
        # DR-024: file edits are journalled and revertible by default; a
        # workspace that would rather be asked first opts in here.
        self._approval_policy = ApprovalPolicy.resolve(
            require_edit_approval=require_edit_approval
        )
        self._model_info = model_info
        self._model: ChatModel = create_chat_model(self._inference)
        self._turns: list[Turn] = []
        # DR-021: a repo's `.autose/config.yaml` tuning/prompts apply to every
        # component this session builds — a default `Tuning()` reproduces the
        # values that used to be hardcoded, so an unconfigured repo is unaffected.
        self._tuning = tuning or Tuning()
        self._prompt_overrides = prompt_overrides
        self._memory = SessionMemory(
            compress=self._compress_history,
            max_verbatim=self._tuning.session.max_history_exchanges,
            max_summary_chars=self._tuning.session.max_summary_chars,
        )
        self._tier: str = normalise_tier(default_tier, fallback=SPARK)
        self._usage = Usage()
        # DR-026: no store means nothing is written, which is the default. A
        # store that fails is still not an error — it records the problem and
        # the session carries on.
        self._store: SessionStore = store or NullSessionStore()
        self._id = session_id or new_session_id()
        self._classifier = TaskClassifier(
            self._model,
            context_limit=self.context_limit(),
            tuning=self._tuning.classifier,
            prompt_overrides=self._prompt_overrides,
        )
        self._created_at = time.time()
        # DR-030: one token for the session's lifetime, cleared at the start of
        # every turn. One rather than one-per-turn because an Adapter wires it
        # up once — the dashboard's Stop button holds a reference, the TUI's
        # signal handler closes over it — and a request that arrives between
        # turns must not silently kill the next one.
        self._cancel = CancelToken()
        #: When False, every prompt uses the session's current tier as-is.
        self.auto_classify = auto_classify

    # -- construction ---------------------------------------------------

    @classmethod
    def from_setup(
        cls,
        setup: SessionSetup,
        *,
        events: EventSink | None = None,
        approval_gate: ApprovalGate | None = None,
        default_tier: str = SPARK,
        auto_classify: bool = True,
        store: SessionStore | None = None,
        resume: SessionRecord | None = None,
    ) -> Session:
        if setup.inference is None or not setup.inference.is_ready:
            raise ConfigurationError(
                "cannot start a session before a runtime and model are resolved"
            )
        gate = approval_gate
        if gate is None and setup.config.auto_approve:
            gate = AutoApproveGate(
                authorized_by=setup.config.auto_approve_authorized_by
            )
        session = cls(
            inference=setup.inference,
            workspace=Workspace.at(setup.config.resolve_workspace_root()),
            events=events,
            approval_gate=gate,
            model_info=setup.model_info,
            default_tier=default_tier,
            auto_classify=auto_classify,
            tuning=setup.config.tuning,
            prompt_overrides=setup.config.prompt_overrides,
            require_edit_approval=setup.config.require_edit_approval,
            store=store,
            session_id=resume.id if resume is not None else "",
        )
        if resume is not None:
            session.restore(resume)
        return session

    # -- introspection ---------------------------------------------------

    @property
    def inference(self) -> InferenceSettings:
        return self._inference

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def model_info(self) -> ModelInfo | None:
        return self._model_info

    @property
    def tier(self) -> str:
        """The session's effective tier — the highest reached so far (DR-003)."""
        return self._tier

    @property
    def history(self) -> tuple[Turn, ...]:
        return tuple(self._turns)

    @property
    def cancel_token(self) -> CancelToken:
        """The stop signal the running turn watches (DR-030)."""
        return self._cancel

    @property
    def stop_requested(self) -> bool:
        return self._cancel.requested

    def request_stop(self, reason: str = "") -> bool:
        """Ask the running turn to stop at its next safe point (DR-030).

        Returns whether this was the first such request, which is how an
        Adapter decides whether to escalate: a second ctrl-c means the user
        does not want to wait for the current round to finish.

        Safe to call from another thread — that is the dashboard's only route
        — and safe to call when nothing is running, since the next turn clears
        the token before it starts.
        """
        first = self._cancel.request(reason)
        if first:
            self._emit(
                "stop_requested",
                reason or "stopping at the next safe point",
                reason=reason,
            )
        return first

    @property
    def memory(self) -> SessionMemory:
        return self._memory

    @property
    def usage(self) -> Usage:
        return self._usage

    @property
    def approval_policy(self) -> ApprovalPolicy:
        return self._approval_policy

    @property
    def session_id(self) -> str:
        return self._id

    @property
    def store(self) -> SessionStore:
        return self._store

    def snapshot(self) -> SessionRecord:
        """This session as plain data, ready to store (DR-026).

        Only what a session already promoted: the distilled `(prompt, answer)`
        pairs and the rolling summary, never a tool transcript or an
        intermediate stage artifact. DR-004's boundary applies to the durable
        copy exactly as it applies to the live one.
        """
        return SessionRecord(
            id=self._id,
            workspace=str(self._workspace.root),
            updated_at=time.time(),
            created_at=self._created_at,
            provider=self._inference.provider,
            base_url=self._inference.base_url,
            model=self._inference.model,
            tier=self._tier,
            summary=self._memory.summary,
            verbatim=tuple(
                (exchange.prompt, exchange.answer)
                for exchange in self._memory.exchanges
            ),
            prompt_tokens=self._usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens,
            turns=tuple(
                TurnRecord(
                    prompt=turn.prompt,
                    answer=turn.answer,
                    tier=turn.tier,
                    stop_reason=turn.stop_reason,
                    prompt_tokens=turn.usage.prompt_tokens,
                    completion_tokens=turn.usage.completion_tokens,
                    files_changed=turn.files_changed,
                    changes=turn.changes,
                )
                for turn in self._turns
            ),
        )

    def restore(self, record: SessionRecord) -> None:
        """Adopt a stored session's history, tier and tally (DR-026).

        The tier goes through the ratchet rather than being assigned, so
        resuming a `craft` session cannot silently drop to `spark` — and a
        session already higher than the record is not pulled down (DR-003).
        """
        self._id = record.id
        self._created_at = record.created_at or self._created_at
        self._turns = [
            Turn(
                prompt=item.prompt,
                answer=item.answer,
                tier=item.tier,
                stop_reason=item.stop_reason,
                usage=Usage(
                    prompt_tokens=item.prompt_tokens,
                    completion_tokens=item.completion_tokens,
                ),
                files_changed=item.files_changed,
                changes=item.changes,
            )
            for item in record.turns
        ]
        self._memory.restore(record.summary, record.verbatim)
        self._usage = Usage(
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
        )
        if record.tier:
            self._tier = ratchet(self._tier, record.tier)
        self._emit(
            "session_resumed",
            record.id,
            session_id=record.id,
            turns=len(self._turns),
            tier=self._tier,
        )

    def persist(self) -> None:
        """Write the session out. Never raises — a failed save is not a failed run."""
        try:
            self._store.save(self.snapshot())
        except Exception as exc:  # noqa: BLE001 - see docstring
            self._emit("session_save_failed", str(exc), error=str(exc))

    @property
    def last_changes(self) -> tuple[FileChange, ...]:
        """What the most recent turn that changed anything actually wrote."""
        turn = self._last_changing_turn()
        return turn.changes if turn is not None else ()

    @property
    def implemented_tiers(self) -> tuple[str, ...]:
        return available_tiers()

    def context_limit(self) -> int | None:
        if self._inference.context_limit:
            return self._inference.context_limit
        return self._model_info.context_length if self._model_info else None

    def available_models(self) -> tuple[ModelInfo, ...]:
        """Re-enumerate the current endpoint. Empty if it cannot be reached."""
        try:
            return tuple(create_model_catalog(self._inference).list_models())
        except Exception:  # noqa: BLE001 - enumeration is a convenience
            return ()

    # -- mutation ---------------------------------------------------------

    def select_model(self, name: str) -> None:
        """Switch models mid-session, adopting the new model's context limit."""
        info = next(
            (model for model in self.available_models() if model.name == name), None
        )
        inference = self._inference.with_model(name)
        if info is not None and info.context_length:
            inference = inference.with_context_limit(info.context_length)
        self._inference = inference.require_model()
        self._model_info = info
        self._model = create_chat_model(self._inference)
        self._emit(
            "model_selected", name, model=name, provider=self._inference.provider
        )

    def request_tier(self, tier: str) -> str:
        """Ask for a tier. The ratchet may return a higher one (DR-003)."""
        previous = self._tier
        self._tier = ratchet(self._tier, tier)
        if self._tier != previous:
            self._emit(
                "tier_changed",
                f"{previous} -> {self._tier}",
                previous=previous,
                tier=self._tier,
            )
        return self._tier

    def clear_history(self) -> None:
        self._turns.clear()
        self._memory.clear()
        self._emit("history_cleared")
        self.persist()

    def undo_last_change(self) -> RevertReport | None:
        """Put the workspace back as it was before the last turn that wrote (DR-024).

        ``None`` means there is nothing to undo. Otherwise the report names
        what was restored and — importantly — what was deliberately left
        alone because it had changed since AutoSE wrote it.
        """
        turn = self._last_changing_turn()
        if turn is None:
            return None
        journal = ChangeJournal.from_changes(self._workspace, turn.changes)
        report = journal.revert()
        # The turn's record is replaced by what survives, so a second undo
        # retries only what the first one could not restore rather than
        # re-reverting files already put back.
        remaining = tuple(
            change for change in turn.changes if change.path not in set(report.restored)
        )
        self._turns[self._turns.index(turn)] = replace(turn, changes=remaining)
        self._emit(
            "changes_reverted",
            f"restored {len(report.restored)} file(s)",
            restored=list(report.restored),
            skipped=[list(item) for item in report.skipped],
        )
        self.persist()
        return report

    def _last_changing_turn(self) -> Turn | None:
        return next(
            (turn for turn in reversed(self._turns) if turn.changes), None
        )

    def classify(self, prompt: str) -> Classification:
        """Pick a tier for *prompt* without running it (DR-003)."""
        return self._classifier.classify(prompt, history=self._memory.build_context())

    # -- running ------------------------------------------------------------

    def run(self, prompt: str, *, tier: str | None = None) -> Turn:
        """Run one prompt to completion."""
        stream = self.stream(prompt, tier=tier)
        for _ in stream:
            pass
        assert stream.turn is not None
        return stream.turn

    def stream(self, prompt: str, *, tier: str | None = None) -> TurnStream:
        """Run one prompt, yielding answer text as it is produced.

        With no explicit *tier*, the prompt is classified and the result fed
        through the session ratchet — so a classification lower than the tier
        the session has already reached is ignored (DR-003).
        """
        # DR-030: a stop asked for while the session was idle — a stray
        # ctrl-c, a Stop clicked as the last turn finished — must not cancel
        # the turn the user is asking for now.
        self._cancel.clear()
        classification: Classification | None = None
        if tier:
            effective = self.request_tier(tier)
        elif self.auto_classify:
            self._emit("classification_started", prompt)
            classification = self.classify(prompt)
            effective = self.request_tier(classification.tier)
            # Emitted *after* the ratchet so the event describes the outcome,
            # not an intermediate: an Adapter can render "spark → craft" in one
            # line instead of announcing a tier that was then overridden.
            self._emit(
                "classification_finished",
                effective,
                tier=classification.tier,
                effective_tier=effective,
                ratcheted=effective != classification.tier,
                source=classification.source,
                detail=classification.detail,
            )
        else:
            effective = self._tier
        holder: dict[str, Turn] = {}
        return TurnStream(self._run(prompt, effective, holder, classification), holder)

    # -- internals -----------------------------------------------------------

    def _run(
        self,
        prompt: str,
        tier: str,
        holder: dict[str, Turn],
        classification: Classification | None = None,
    ) -> Generator[str, None, None]:
        if not supports_tier(tier):
            raise UnavailableTierError(
                f"the '{tier}' tier ({TIER_SPECS[tier].summary}) is not implemented "
                f"yet. Implemented: {', '.join(available_tiers()) or 'none'}."
            )
        self._require_usable_context()
        workflow = create_workflow(tier)
        # One journal per turn: undo restores what *this* prompt did, not an
        # accumulation across the session (DR-024).
        journal = ChangeJournal(self._workspace)
        request = WorkflowRequest(
            prompt=prompt,
            workspace=self._workspace,
            model=self._model,
            events=self._events,
            approval_gate=self._approval_gate,
            approval_policy=self._approval_policy,
            cancel=self._cancel,
            journal=journal,
            session_context=tuple(self._memory.build_context()),
            context_limit=self.context_limit(),
            tuning=self._tuning,
            prompt_overrides=self._prompt_overrides or {},
        )
        self._emit("turn_started", prompt, tier=tier, prompt_text=prompt)
        outcome: WorkflowOutcome = yield from workflow.stream(request)
        turn = Turn(
            prompt=prompt,
            answer=outcome.answer,
            tier=tier,
            stop_reason=outcome.stop_reason,
            usage=outcome.usage,
            classification=classification,
            artifacts=dict(outcome.artifacts),
            files_changed=outcome.files_changed,
            changes=outcome.changes,
        )
        self._record(turn)
        holder["turn"] = turn
        self._emit(
            "turn_finished",
            outcome.stop_reason,
            tier=tier,
            stop_reason=outcome.stop_reason,
            succeeded=outcome.succeeded,
            total_tokens=turn.usage.total_tokens,
            files_changed=list(turn.files_changed),
        )

    def _require_usable_context(self) -> None:
        """Refuse a turn the served context window cannot carry (DR-022).

        Checked per turn rather than at construction on purpose: the way out
        of this is usually to pick a different model, and refusing to build
        the session would take the model picker away along with the problem.
        A session in this state stays fully usable for everything except
        running work.

        Fails closed because the alternative is worse than an error. With too
        small a window the budget evicts each round's tool results to make
        room for the next, the agent re-reads what it can no longer see, and
        the no-progress guard stops it — reporting a stalled model. The user
        then changes model, prompt and tier in turn, none of which is the
        problem.
        """
        floor = count_limit(self._tuning.context.min_usable_context_tokens)
        limit = self.context_limit()
        if floor is None or limit is None or limit >= floor:
            return
        info = self._model_info
        if info is not None and not info.context_length_measured:
            # An assumed window, not an observed one. Ollama publishes the
            # window it is serving only for a model that is already loaded, so
            # a cold server started with a raised `OLLAMA_CONTEXT_LENGTH` still
            # looks like the 4096 default from here. Refusing on that guess
            # would lock the user out of a server that was configured
            # correctly all along. The pre-flight warning still stands, and a
            # window that really is this small announces itself the moment the
            # first turn loads the model.
            return
        capped = info is not None and info.is_context_capped
        remedy = (
            f"The model itself supports {info.max_context_length:,} tokens — the "
            f"runtime is what is serving {limit:,}. Raise it with "
            f"{OLLAMA_CONTEXT_ENV}, e.g. `{OLLAMA_CONTEXT_ENV}=32768 ollama serve`."
            if capped and info is not None and info.max_context_length
            else "Pick a model served in a larger window."
        )
        raise ConfigurationError(
            f"The context window is too small to work in: {limit:,} tokens, "
            f"below the {floor:,} AutoSE needs. Tool schemas, the system prompt "
            "and reserved output consume most of that before your request is "
            "counted, so each round would discard the previous round's file "
            f"reads and the run would stop as stalled. {remedy} "
            "To run anyway, set `tuning.context.min_usable_context_tokens: -1`."
        )

    def _record(self, turn: Turn) -> None:
        """Promote the final (prompt, answer) pair only (DR-004).

        Intermediate loop state — tool transcripts, per-stage artifacts, the
        requirements ledger mid-flight — never reaches session memory, which is
        what keeps a tool-call transcript structurally unable to leak into
        long-term history.

        A turn that did not complete (``cancelled``, ``round_cap``,
        ``empty_answer``, ...) is still kept in ``self._turns`` for display and
        undo, but is *not* fed into session memory: its answer is a partial
        draft, not an answer, and handing it back as prior conversation on the
        next turn reads to the model as unfinished work to resume rather than
        as closed history — which is exactly what derails a small local model
        into carrying an abandoned line of reasoning into an unrelated next
        prompt.
        """
        self._turns.append(turn)
        if turn.succeeded:
            self._memory.record(turn.prompt, turn.answer)
        self._usage = Usage(
            prompt_tokens=self._usage.prompt_tokens + turn.usage.prompt_tokens,
            completion_tokens=self._usage.completion_tokens
            + turn.usage.completion_tokens,
        )
        # After every turn, so an interrupted session loses at most the turn
        # that was still running (DR-026).
        self.persist()

    def _compress_history(self, existing_summary: str, exchange: str) -> str:
        """Roll an overflowing exchange into the session summary (DR-004).

        A failure here is not retried: :class:`SessionMemory` drops the oldest
        exchange and records that it did, rather than looping on a summariser
        that is not working.
        """
        history = "\n\n".join(part for part in (existing_summary, exchange) if part)
        result = self._model.complete(
            [{"role": "user", "content": _SUMMARY_PROMPT.format(history=history)}],
            max_output_tokens=_SUMMARY_MAX_TOKENS,
        )
        return result.content

    def _emit(self, event_type: str, message: str = "", **data) -> None:
        self._events.emit(Event(type=event_type, message=message, data=data))


class TurnStream:
    """An in-progress turn: iterate for text, then read :attr:`turn`."""

    def __init__(self, chunks: Iterator[str], holder: dict[str, Turn]) -> None:
        self._chunks = chunks
        self._holder = holder

    def __iter__(self) -> Iterator[str]:
        yield from self._chunks

    @property
    def turn(self) -> Turn | None:
        return self._holder.get("turn")


# -- helpers ------------------------------------------------------------------


def _enumerate(settings: InferenceSettings) -> DetectedRuntime | None:
    """Best-effort enumeration of an explicitly configured endpoint."""
    try:
        models = create_model_catalog(settings).list_models()
    except Exception:  # noqa: BLE001 - never block an explicit configuration
        return None
    return DetectedRuntime(
        provider=settings.provider,
        base_url=settings.base_url,
        reachable=True,
        models=models,
    )


def _capability_warnings(info: ModelInfo | None) -> tuple[str, ...]:
    """Pre-flight checks DR-015 wants surfaced before work starts."""
    if info is None:
        return ()
    warnings: list[str] = []
    if info.supports_tools is False:
        warnings.append(
            f"'{info.name}' reports no tool-calling support. AutoSE needs tools to "
            "read and change files — pick a tool-capable model, or expect it to "
            "answer from the prompt alone."
        )
    # DR-022: the gap between what the model supports and what the runtime
    # actually serves is invisible otherwise — the request is not rejected,
    # it is silently truncated from the front.
    if info.is_context_capped:
        assert info.context_length is not None and info.max_context_length is not None
        warnings.append(
            f"'{info.name}' supports {info.max_context_length:,} tokens but the "
            f"runtime is serving only {info.context_length:,}. AutoSE will budget "
            "against the served window; anything larger would be silently dropped "
            f"by the runtime. Raise it with {OLLAMA_CONTEXT_ENV}, e.g. "
            f"`{OLLAMA_CONTEXT_ENV}=32768 ollama serve`."
        )
    if info.context_length and info.context_length < MIN_USABLE_CONTEXT_TOKENS:
        # Not "requests get trimmed" — with a truthful budget they fail closed
        # (DR-010), and the user deserves the number that makes that make sense.
        warnings.append(
            f"a {info.context_length:,}-token window is below the "
            f"{MIN_USABLE_CONTEXT_TOKENS:,} AutoSE needs to work usefully. Tool "
            "schemas and the stage prompt take most of it before your request is "
            "counted, so reading even one file may exceed the budget and stop the "
            "stage rather than silently truncating it."
        )
    return tuple(warnings)


def _no_runtime_message(runtimes: tuple[DetectedRuntime, ...]) -> str:
    reachable = [runtime for runtime in runtimes if runtime.reachable]
    if reachable:
        endpoints = ", ".join(runtime.base_url for runtime in reachable)
        return (
            f"a runtime answered at {endpoints} but is serving no models. "
            "Pull one (e.g. `ollama pull qwen3:8b`) and try again."
        )
    probed = ", ".join(
        provider.default_base_url
        for provider in local_providers()
        if provider.default_base_url
    )
    return (
        "no local model runtime found. AutoSE probed: "
        f"{probed}. Start one (e.g. `ollama serve`), or configure a different "
        "endpoint with AUTOSE_BASE_URL or a config file."
    )
