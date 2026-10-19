"""The terminal Adapter (DR-002).

Startup, the read-run-render loop, and rendering. Everything it knows how to
*do* — discover a runtime, classify and dispatch a prompt, gate a tool call,
bound a context window — it does by calling ``logic.session.Session``. Nothing
here decides behaviour; swap this module for another and the agent behaves
identically, which is the property that makes a second interface (the
DR-029 dashboard) an addition rather than a fork.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path

try:  # pragma: no cover - present on every POSIX build, absent on some Windows
    # Imported for its effect, not its API: once loaded, built-in ``input()``
    # gains line editing and in-session history, which a prompt people type
    # whole paragraphs into badly needs. It also lets a request the user typed
    # while the model was working be handed back to them editable.
    import readline
except ImportError:  # pragma: no cover
    # Deliberately rebinding the name to None: every use below guards on it,
    # and an optional module is simpler to reason about than a stub.
    readline = None  # type: ignore[assignment]

from dataclasses import replace

from rich.console import Console
from rich.text import Text

from logic import __version__
from logic.common.events import CompositeEventSink, EventSink
from logic.common.processes import exit_cleanly_on_termination
from logic.config import AutoSEConfig, load_config
from logic.errors import AutoSEError, ConfigurationError
from logic.persistence import SESSIONS_DIRECTORY, FileSessionStore, SessionRecord
from logic.saved_allowances import AllowanceFile
from logic.session import Session, SessionSetup, setup_from_config
from logic.tiers import SPARK
from logic.workflows import UnavailableTierError

from .activity import ActivityIndicator
from .approval import TerminalApprovalGate
from .commands import dispatch, is_command
from .interrupt import stop_on_interrupt
from .render import (
    answer_markdown,
    banner,
    changes_summary,
    error_panel,
    format_tokens,
    outcome_panel,
    runtime_table,
    session_table,
    status_line,
    warning_text,
)
from .theme import THEME

__all__ = ["App", "run", "print_sessions"]


class App:
    """One terminal session."""

    def __init__(
        self,
        session: Session,
        *,
        console: Console,
        approval_gate: TerminalApprovalGate,
        activity: ActivityIndicator,
    ) -> None:
        self.session = session
        self.console = console
        self.approval_gate = approval_gate
        self.activity = activity
        #: Text typed while the last turn was running, waiting to be offered
        #: back at the next prompt rather than discarded.
        self._carried = ""

    # -- the loop ---------------------------------------------------------

    def run(self, initial_prompt: str = "") -> int:
        self.print_status()
        self.console.print(
            Text(
                f"  type a request, or /help for commands  {THEME.bullet}  "
                f"end a line with \\ to continue it  {THEME.bullet}  "
                "ctrl-o expands the thinking block  "
                f"{THEME.bullet}  ctrl-c stops the run, ctrl-d exits",
                style=THEME.muted,
            )
        )
        self.console.print()

        pending = initial_prompt.strip()
        while True:
            line = pending or self._read()
            pending = ""
            if line is None:
                return 0
            if not line:
                continue
            if is_command(line):
                if not dispatch(self, line):
                    return 0
                self.console.print()
                continue
            self._turn(line)

    def _read(self) -> str | None:
        """Read one request. ``None`` means the user is done.

        A request may span several lines: a trailing backslash continues it,
        which is the difference between describing a task properly and
        flattening it into one line because the prompt submits on Enter.
        """
        lines: list[str] = []
        while True:
            marker = THEME.prompt if not lines else THEME.continuation
            style = f"bold {THEME.accent}" if not lines else THEME.muted
            try:
                line = self._input(f"[{style}]{marker}[/] ", self._take_carried())
            except EOFError:
                self.console.print()
                # A half-typed request is not a request; submitting it on the
                # way out would run something the user never finished asking.
                return None if not lines else ""
            except KeyboardInterrupt:
                self.console.print(
                    Text(
                        "  (ctrl-d to exit)" if not lines else "  (discarded)",
                        style=THEME.muted,
                    )
                )
                return ""
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                lines.append(stripped[:-1].rstrip())
                continue
            lines.append(line)
            return "\n".join(lines).strip()

    def _input(self, prompt: str, prefill: str = "") -> str:
        """Read a line, starting it with *prefill* where the terminal allows.

        Anything typed during a turn is text the user meant for the prompt.
        Discarding it would punish them for typing ahead; submitting it blind
        would run something they never saw. Pre-filling the line does neither:
        it is there, and it is theirs to edit.
        """
        if not prefill or readline is None:
            if prefill:
                # No line editor to hand it to; show it rather than lose it.
                self.console.print(
                    Text(f"  you typed: {prefill}", style=THEME.muted)
                )
            return self.console.input(prompt)
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
        try:
            return self.console.input(prompt)
        finally:
            readline.set_startup_hook()

    def _take_carried(self) -> str:
        carried, self._carried = self._carried, ""
        return carried

    def _turn(self, prompt: str) -> None:
        self.console.print()
        self.activity.start("thinking")
        collected: list[str] = []
        try:
            # DR-030: nothing else bounds the run by default, so ctrl-c is the
            # stop. The first press unwinds the turn at its next safe point and
            # still renders the partial answer, what was changed, and how to
            # undo it; a second press aborts as it always did.
            with stop_on_interrupt(
                self.session.request_stop, announce=self._announce_stop
            ):
                stream = self.session.stream(prompt)
                for chunk in stream:
                    if self.activity.staged:
                        # A multi-stage run reports through stage events; its
                        # final text is a summary to render at the end, not a
                        # live stream.
                        collected.append(chunk)
                        continue
                    if not collected:
                        self.activity.begin_answer()
                    self.console.print(chunk, end="", markup=False, highlight=False)
                    collected.append(chunk)
            self._end_activity()
            self._render_answer(stream.turn, collected)
            self._render_outcome(
                stream.turn,
                streamed=bool(collected) and not self.activity.staged,
            )
        except KeyboardInterrupt:
            self._end_activity()
            self.console.print()
            self.console.print(
                Text(
                "  aborted — this turn was not recorded, so /undo cannot reach "
                "anything it changed",
                style=THEME.warning,
            )
            )
        except UnavailableTierError as exc:
            self._end_activity()
            self.console.print(error_panel(str(exc), title="Not implemented yet"))
        except AutoSEError as exc:
            self._end_activity()
            self.console.print(error_panel(str(exc), title=type(exc).__name__))
        finally:
            self.activity.stop()
        self.console.print()

    def _end_activity(self) -> None:
        """Close the live indicator, keeping whatever it was mid-thought."""
        self.activity.finish_turn()
        self.activity.stop()
        self._carried = self.activity.take_typed()

    def _announce_stop(self, first: bool) -> None:
        """Say what a ctrl-c just did, so the keypress does not look ignored.

        Printed through the activity indicator rather than the console: the
        indicator owns the bottom of the screen while a turn runs, and writing
        under it directly would be overwritten on its next refresh.
        """
        if not first:  # pragma: no cover - the handler aborts before this
            return
        self.activity.note(
            "stopping after the current step — ctrl-c again to abort now"
        )

    def _render_answer(self, turn, collected: list[str]) -> None:
        """Show the answer, as markdown when it was not streamed live."""
        if turn is None:
            return
        if self.activity.staged or not collected:
            # A staged run's summary, or a provider that streamed nothing:
            # render it as markdown rather than raw text.
            if turn.succeeded and turn.answer:
                self.console.print()
                self.console.print(answer_markdown(turn.answer))
        else:
            self.console.print()

    def _render_outcome(self, turn, *, streamed: bool = False) -> None:
        """Report how the turn ended — never dressing a stop as a success.

        *streamed* says the answer already went to the screen token by token,
        so the panel below points at it rather than repeating it.
        """
        if turn is None:
            return
        if turn.changes:
            # DR-024: what was written, how big each change was, and how to
            # review or undo it — shown whether or not the turn completed,
            # because a run that stopped halfway is exactly when a user needs
            # to know what already landed.
            self.console.print()
            self.console.print(changes_summary(turn.changes))
        if not turn.succeeded:
            self.console.print()
            self.console.print(
                outcome_panel(
                    turn.stop_reason, turn.answer or "", already_shown=streamed
                )
            )
            if turn.stop_reason == "empty_answer":
                self.console.print(
                    warning_text(
                        "the model spent its whole output budget reasoning. Try a "
                        "narrower request, or a model with more room to answer."
                    )
                )
            if turn.stop_reason == "cancelled":
                self.console.print(
                    warning_text(
                        "you stopped this run. What it had already produced is "
                        "above and in the session history — ask a follow-up to "
                        "carry on from here."
                    )
                )
        if turn.usage.total_tokens:
            self.console.print(
                Text(
                    f"  {format_tokens(turn.usage.total_tokens)} tokens "
                    f"{THEME.bullet} {self.activity.elapsed:.0f}s "
                    f"{THEME.bullet} {turn.tier}",
                    style=THEME.muted,
                )
            )

    def print_status(self) -> None:
        self.console.print(status_line(self.session))


# -- startup -------------------------------------------------------------------


def run(
    *,
    config_path: str | Path | None = None,
    workspace: str | Path | None = None,
    initial_prompt: str = "",
    auto_approve: bool = False,
    tier: str | None = None,
    resume: str | None = None,
    console: Console | None = None,
) -> int:
    """Start the terminal interface. Returns a process exit code."""
    console = console or Console(highlight=False)
    console.print(banner(__version__))

    try:
        config = load_config(config_path, workspace_root=workspace)
    except ConfigurationError as exc:
        console.print(error_panel(str(exc), title="Configuration"))
        return 2
    for message in config.warnings:
        console.print(warning_text(message))
    if workspace is not None or auto_approve:
        # DR-012: --yes is a deliberate per-run opt-in, and it is attributed to
        # whoever invoked it, so an auto-approved call stays accountable in the
        # event log rather than appearing to have been approved by nobody.
        # `replace` rather than reconstructing the dataclass by hand, so any
        # field added to AutoSEConfig later (e.g. DR-021's tuning/prompt
        # overrides) is preserved here without this call needing to change.
        config = replace(
            config,
            workspace_root=Path(workspace) if workspace is not None else config.workspace_root,
            auto_approve=config.auto_approve or auto_approve,
            auto_approve_authorized_by=(
                config.auto_approve_authorized_by or _invoking_user()
            ),
        )

    setup = _resolve(console, config)
    if setup is None:
        return 1

    # DR-057: the rules for every session in this workspace, from the user's
    # config directory rather than from anything in the workspace.
    approval_gate = TerminalApprovalGate(
        console,
        workspace_rules=AllowanceFile.for_workspace(setup.config.resolve_workspace_root()),
    )
    activity = ActivityIndicator(console)
    events: EventSink = CompositeEventSink(activity)

    store = _store_for(setup.config.resolve_workspace_root())
    record: SessionRecord | None = None
    if resume:
        record = _resolve_resume(console, store, resume)
        if record is None:
            return 1

    try:
        session = Session.from_setup(
            setup,
            events=events,
            approval_gate=None if setup.config.auto_approve else approval_gate,
            default_tier=tier or SPARK,
            auto_classify=tier is None,
            store=store,
            resume=record,
        )
    except AutoSEError as exc:
        console.print(error_panel(str(exc), title="Could not start"))
        return 1

    # DR-030: the gate has to know when the run is stopping, or a ctrl-c pressed
    # while an approval prompt is up would sit there waiting for an answer to a
    # call that is no longer going to run.
    approval_gate.watch_for_stop(lambda: session.stop_requested)
    # DR-057: a resumed session's own rules come back with it, and every
    # "always allow" is saved with it.
    approval_gate.adopt(session.allowances, keep=session.keep_allowances)

    if record is not None:
        console.print(
            Text(
                f"  {THEME.ok_glyph} resumed session {record.id} "
                f"({len(record.turns)} turn(s), tier {session.tier})",
                style=THEME.success,
            )
        )
        _report_restored_rules(console, session)

    if setup.config.auto_approve:
        console.print(
            warning_text(
                "auto-approve is on: shell and network calls will run without "
                f"asking (authorized by {setup.config.auto_approve_authorized_by})"
            )
        )
    if setup.config.require_edit_approval:
        console.print(
            warning_text(
                "this workspace asks before every file write "
                "(approval.require_edit_approval)"
            )
        )
    try:
        # DR-062: a closed terminal window (SIGHUP) still runs the `finally`.
        with exit_cleanly_on_termination():
            return App(
                session, console=console, approval_gate=approval_gate, activity=activity
            ).run(initial_prompt)
    finally:
        # DR-062: a server a run started does not outlive the app that shows it.
        session.close()


def print_sessions(*, workspace: str | Path | None = None) -> int:
    """List this workspace's stored sessions, and exit (DR-026)."""
    console = Console(highlight=False)
    root = Path(workspace).expanduser().resolve() if workspace else Path.cwd()
    store = _store_for(root)
    records = store.list()
    console.print(session_table(records))
    for problem in store.problems:
        console.print(warning_text(problem))
    return 0


def _report_restored_rules(console: Console, session: Session) -> None:
    """Say what a resumed session runs without asking, and what it dropped."""
    if session.allowances:
        labels = ", ".join(item.label for item in session.allowances)
        console.print(Text(f"  {THEME.ok_glyph} still allowed: {labels}", style=THEME.muted))
    for note in session.dropped_allowances:
        console.print(warning_text(f"will ask again: {note}"))


def _store_for(workspace_root: Path) -> FileSessionStore:
    return FileSessionStore(workspace_root / SESSIONS_DIRECTORY)


def _resolve_resume(
    console: Console, store: FileSessionStore, reference: str
) -> SessionRecord | None:
    """Find the session --resume named, or explain what is available instead."""
    record = store.latest() if reference == "latest" else store.resolve(reference)
    if record is not None:
        return record
    if reference == "latest":
        console.print(
            error_panel(
                "there is no stored session for this workspace to resume.",
                title="Nothing to resume",
            )
        )
    else:
        console.print(
            error_panel(
                f"no stored session matches '{reference}'.",
                title="Nothing to resume",
            )
        )
        console.print(session_table(store.list(limit=10)))
    return None


def _resolve(console: Console, config: AutoSEConfig) -> SessionSetup | None:
    """Find a runtime and a model, asking only about what cannot be inferred."""
    with console.status(
        Text("looking for a local model runtime…", style=THEME.muted), spinner="dots"
    ):
        setup = setup_from_config(config)

    if not setup.is_ready:
        for message in setup.warnings:
            console.print(error_panel(message, title="No model available"))
        if setup.runtimes:
            console.print(runtime_table(setup.runtimes))
        return None

    # `is_ready` above is exactly the check that an endpoint was resolved;
    # binding it here makes that a fact the reader and the checker share.
    inference = setup.inference
    assert inference is not None

    runtime = setup.runtime_for(inference.base_url)
    detail = Text()
    detail.append(f"  {THEME.ok_glyph} ", style=THEME.success)
    detail.append(inference.provider, style=THEME.accent)
    detail.append(f" at {inference.base_url}", style=THEME.muted)
    if runtime is not None and len(runtime.models) > 1:
        detail.append(f"  ({len(runtime.models)} models — /model to switch)", style=THEME.muted)
    console.print(detail)

    for message in setup.warnings:
        console.print(warning_text(message))
    console.print()
    return setup


def _invoking_user() -> str:
    """Who to attribute an auto-approved run to (DR-012)."""
    try:
        return f"{getpass.getuser()} (--yes)"
    except Exception:  # noqa: BLE001 - no controlling user in some environments
        return f"{os.environ.get('USER', 'unknown')} (--yes)"
