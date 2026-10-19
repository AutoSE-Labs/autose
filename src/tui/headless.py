"""The non-interactive Adapter: one prompt in, JSON out (DR-027).

A second Adapter over the same ``logic.session.Session``, and the reason it
exists is not only that scripts want JSON. DR-002 claims the terminal
interface is a *thin* adapter with no business logic in it; a second,
completely different presentation over the identical facade is the only way to
actually test that claim, and it found the places where it was not yet true.

Every line written to stdout is one JSON object, newline-delimited, so a
consumer can process the stream as it arrives rather than waiting for a whole
document. Events pass through as they are emitted; the last line is always a
single ``result`` (or ``error``) object.

Nothing here decides anything. It serialises what ``logic`` emits and returns
an exit code derived from the outcome the session reported — including DR-009's
rule that a run which hit a bound is not a success, and DR-030's that a run
someone stopped is not either, which shows up here as a non-zero exit rather
than as prose a script would have to parse.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from logic import __version__
from logic.common.events import CallbackEventSink, Event, EventSink
from logic.config import load_config
from logic.errors import AutoSEError, ConfigurationError
from logic.persistence import SESSIONS_DIRECTORY, FileSessionStore, SessionRecord
from logic.session import Session, setup_from_config
from logic.tiers import SPARK

from .interrupt import stop_on_interrupt

__all__ = ["run_headless", "EXIT_OK", "EXIT_INCOMPLETE", "EXIT_CONFIGURATION"]

#: The turn completed.
EXIT_OK = 0
#: The run finished but did not complete — a bound was hit, a stage failed, no
#: runtime was available. DR-009: never reported as success.
EXIT_INCOMPLETE = 1
#: AutoSE could not be configured or started at all.
EXIT_CONFIGURATION = 2


def run_headless(
    prompt: str,
    *,
    config_path: str | Path | None = None,
    workspace: str | Path | None = None,
    auto_approve: bool = False,
    authorized_by: str = "",
    tier: str | None = None,
    resume: str | None = None,
    stream: TextIO | None = None,
) -> int:
    """Run one prompt, writing newline-delimited JSON. Returns an exit code."""
    out = stream or sys.stdout
    write = _writer(out)

    if not prompt.strip():
        write({"type": "error", "error": "no prompt given", "kind": "usage"})
        return EXIT_CONFIGURATION

    try:
        config = load_config(config_path, workspace_root=workspace)
    except ConfigurationError as exc:
        write({"type": "error", "error": str(exc), "kind": "configuration"})
        return EXIT_CONFIGURATION

    if workspace is not None or auto_approve:
        # DR-012: unattended approval stays a deliberate, attributed opt-in
        # even when no human is watching — arguably especially then.
        config = replace(
            config,
            workspace_root=Path(workspace) if workspace is not None else config.workspace_root,
            auto_approve=config.auto_approve or auto_approve,
            auto_approve_authorized_by=(
                config.auto_approve_authorized_by or authorized_by or "--json --yes"
            ),
        )

    write(
        {
            "type": "started",
            "version": __version__,
            "workspace": str(config.resolve_workspace_root()),
            "prompt": prompt,
            "auto_approve": config.auto_approve,
        }
    )
    for message in config.warnings:
        write({"type": "warning", "message": message})

    setup = setup_from_config(config)
    if not setup.is_ready:
        for message in setup.warnings:
            write({"type": "error", "error": message, "kind": "no_runtime"})
        return EXIT_INCOMPLETE
    for message in setup.warnings:
        write({"type": "warning", "message": message})

    store = FileSessionStore(config.resolve_workspace_root() / SESSIONS_DIRECTORY)
    record = _resume_record(store, resume)
    if resume and record is None:
        write(
            {
                "type": "error",
                "error": f"no stored session matches '{resume}'",
                "kind": "no_session",
            }
        )
        return EXIT_CONFIGURATION

    events: EventSink = CallbackEventSink(lambda event: write(_event(event)))
    try:
        session = Session.from_setup(
            setup,
            events=events,
            # DR-012: with no approval interface attached, the default gate
            # refuses. That is the correct posture for an unattended run —
            # `--yes` is how a caller opts out, deliberately and on the record.
            approval_gate=None,
            default_tier=tier or SPARK,
            auto_classify=tier is None,
            store=store,
            resume=record,
        )
    except AutoSEError as exc:
        write({"type": "error", "error": str(exc), "kind": type(exc).__name__})
        return EXIT_CONFIGURATION

    try:
        # DR-030: with no caps to stop it, a piped run needs an ordered
        # shutdown too — SIGINT asks the turn to stop and still emits a proper
        # `result` line (with `stop_reason: cancelled` and whatever was
        # changed), so a script can act on a stopped run instead of parsing a
        # broken stream. A second SIGINT falls through to the abort below.
        with stop_on_interrupt(session.request_stop):
            turn = session.run(prompt, tier=tier)
    except AutoSEError as exc:
        write({"type": "error", "error": str(exc), "kind": type(exc).__name__})
        return EXIT_INCOMPLETE
    except KeyboardInterrupt:
        write({"type": "error", "error": "interrupted", "kind": "interrupted"})
        return EXIT_INCOMPLETE
    finally:
        # DR-062: one prompt, one process: nothing it started is left behind.
        session.close()

    write(_result(turn, session))
    return EXIT_OK if turn.succeeded else EXIT_INCOMPLETE


# -- serialisation ------------------------------------------------------------


def _writer(out: TextIO):
    """One JSON object per line, flushed, so a consumer can read as it goes."""

    def write(payload: dict[str, Any]) -> None:
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - see below
            # A payload that will not serialise must not take down the run, and
            # the escape hatch is wider than it looks: `default=str` calls the
            # object's own `__str__`, which is arbitrary code and can raise
            # anything at all. Catching only TypeError/ValueError would let a
            # tool result with a hostile repr kill the stream.
            line = json.dumps(
                {"type": "warning", "message": "an event could not be serialised"}
            )
        out.write(line + "\n")
        try:
            out.flush()
        except (OSError, ValueError):  # pragma: no cover - closed stream
            pass

    return write


def _event(event: Event) -> dict[str, Any]:
    return {
        "type": "event",
        "event": event.type,
        "message": event.message,
        "data": dict(event.data),
        "timestamp": event.timestamp,
    }


def _result(turn, session: Session) -> dict[str, Any]:
    return {
        "type": "result",
        "ok": turn.succeeded,
        "tier": turn.tier,
        "stop_reason": turn.stop_reason,
        "answer": turn.answer,
        "session_id": session.session_id,
        "files_changed": list(turn.files_changed),
        "changes": [_change(change) for change in turn.changes],
        "usage": {
            "prompt_tokens": turn.usage.prompt_tokens,
            "completion_tokens": turn.usage.completion_tokens,
            "total_tokens": turn.usage.total_tokens,
        },
        "classification": (
            {
                "tier": turn.classification.tier,
                "source": turn.classification.source,
                "detail": turn.classification.detail,
            }
            if turn.classification is not None
            else None
        ),
    }


def _change(change) -> dict[str, Any]:
    """A change as metadata plus a diff — never the raw before/after bytes.

    A consumer wants to know what happened, and a diff says that in a form
    both a human and a patch tool can read. Emitting both halves verbatim
    would put whole file contents on stdout, which is a different feature.
    """
    added, removed = change.stats
    return {
        "path": change.path,
        "kind": change.kind,
        "tool": change.tool,
        "added": added,
        "removed": removed,
        "restorable": change.restorable,
        "diff": change.diff(),
    }


def _resume_record(store: FileSessionStore, resume: str | None) -> SessionRecord | None:
    if not resume:
        return None
    return store.latest() if resume == "latest" else store.resolve(resume)
