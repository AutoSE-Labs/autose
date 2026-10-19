"""Turning what ``logic`` returns into JSON the browser can render.

Serialisation only. Every function here takes a value the logic layer already
computed and flattens it — no decisions, no formatting judgements the terminal
Adapter would make differently, no field the dashboard invents. When a payload
is deliberately narrower than the object behind it, the reason is that the
wider version would put whole file contents on a socket (see
:func:`change_payload`) or the model's entire design document into a state
poll (see :data:`ARTIFACT_CHARS`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from logic.common.changes import FileChange, RevertReport
from logic.common.events import Event
from logic.connections import Connection
from logic.diagnostics import Diagnosis
from logic.inference.catalog import ModelInfo
from logic.inference.discovery import DetectedRuntime
from logic.persistence import SessionRecord
from logic.session import Turn
from logic.tiers import TIER_SPECS

__all__ = [
    "ARTIFACT_CHARS",
    "change_payload",
    "connection_payload",
    "diagnosis_payload",
    "event_payload",
    "model_payload",
    "record_payload",
    "revert_payload",
    "runtime_payload",
    "session_title",
    "tier_payloads",
    "turn_payload",
    "warnings_payload",
]

#: How much of a stage artifact (a plan, a design note) the dashboard shows.
#: These are model prose and can run long; the full text is in the session
#: file, and the panel that displays them is a reference, not a document view.
ARTIFACT_CHARS = 20_000


def turn_payload(turn: Turn, *, index: int = -1) -> dict[str, Any]:
    """One completed exchange, as the conversation renders it."""
    return {
        "index": index,
        "prompt": turn.prompt,
        "answer": turn.answer,
        "tier": turn.tier,
        "stop_reason": turn.stop_reason,
        "succeeded": turn.succeeded,
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
        "files_changed": list(turn.files_changed),
        "requirements": [dict(item) for item in turn.requirements],
        "route": [dict(item) for item in turn.route],
        "changes": [change_payload(change) for change in turn.changes],
        "undone": [change_payload(change) for change in turn.undone],
        "artifacts": {
            name: _clip(text, ARTIFACT_CHARS)
            for name, text in dict(turn.artifacts).items()
            if text
        },
    }


def change_payload(change: FileChange, *, with_diff: bool = False) -> dict[str, Any]:
    """A journalled file change as metadata, and only on request as a diff.

    The before/after bytes are never sent: a diff says what happened in a form
    a human and a patch tool can both read, whereas shipping both halves of
    every write would stream whole files into a browser tab (DR-024).
    """
    added, removed = change.stats
    payload: dict[str, Any] = {
        "path": change.path,
        "kind": change.kind,
        "tool": change.tool,
        "added": added,
        "removed": removed,
        "restorable": change.restorable,
        "reason": change.reason,
        "is_text": change.is_text,
    }
    if with_diff:
        payload["diff"] = change.diff()
    return payload


def revert_payload(report: RevertReport) -> dict[str, Any]:
    """What an undo put back, and what it deliberately left alone (DR-024)."""
    return {
        "restored": list(report.restored),
        "skipped": [{"path": path, "reason": reason} for path, reason in report.skipped],
        "complete": report.complete,
        "empty": report.empty,
    }


def model_payload(info: ModelInfo | None) -> dict[str, Any] | None:
    if info is None:
        return None
    return {
        "name": info.name,
        "context_length": info.context_length,
        "max_context_length": info.max_context_length,
        "supports_tools": info.supports_tools,
        "is_tool_capable": info.is_tool_capable,
        "is_context_capped": info.is_context_capped,
        "parameter_size": info.parameter_size,
        "quantization": info.quantization,
        "size_bytes": info.size_bytes,
        "family": info.family,
    }


def runtime_payload(runtime: DetectedRuntime) -> dict[str, Any]:
    return {
        "provider": runtime.provider,
        "base_url": runtime.base_url,
        "reachable": runtime.reachable,
        "usable": runtime.usable,
        "error": runtime.error,
        "description": runtime.description,
        "models": [model_payload(model) for model in runtime.models],
    }


def connection_payload(connection: Connection) -> dict[str, Any]:
    """One provider on the settings page (DR-036).

    The key appears only as ``has_key``, its masked tail and where it came
    from. :class:`Connection` holds no key to leak, and this is the only place
    one is turned into JSON, so a test on it covers the whole surface.
    """
    return {
        "provider": connection.provider,
        "description": connection.description,
        "is_local": connection.is_local,
        "requires_api_key": connection.requires_api_key,
        "has_key": connection.key.has_key,
        "masked_key": connection.key.masked,
        "key_source": connection.key.source,
        "base_url": connection.base_url,
        "reachable": connection.reachable,
        "usable": connection.usable,
        "error": connection.error,
        "serves_requested_window": connection.serves_requested_window,
        "models": [model_payload(model) for model in connection.models],
    }


def diagnosis_payload(diagnosis: Diagnosis) -> dict[str, Any]:
    """``autose --doctor`` as a panel rather than a table (DR-002)."""
    return {
        "healthy": diagnosis.healthy,
        "problems": list(diagnosis.problems),
        "advisories": list(diagnosis.advisories),
        "implemented_tiers": list(diagnosis.implemented_tiers),
        "total_models": diagnosis.total_models,
        "runtimes": [runtime_payload(runtime) for runtime in diagnosis.runtimes],
        "configured_url": diagnosis.configured_url,
        "workspace": str(diagnosis.config.resolve_workspace_root()),
        "config_source": str(diagnosis.config.source) if diagnosis.config.source else "",
    }


def record_payload(record: SessionRecord) -> dict[str, Any]:
    """A stored session as one row in the worklog rail (DR-026)."""
    last = record.turns[-1] if record.turns else None
    return {
        "id": record.id,
        "title": record.title,
        "name": record.name,
        # DR-056: a closed session with requests waiting says so in its row.
        "queued": len(record.queue),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "provider": record.provider,
        "model": record.model,
        "tier": record.tier,
        "turns": len(record.turns),
        "total_tokens": record.total_tokens,
        "files_changed": list(record.files_changed),
        "stop_reason": last.stop_reason if last is not None else "",
    }


def session_title(
    turns: Sequence[Turn], current: Mapping[str, Any] | None = None
) -> str:
    """A one-line label for an *open* session, for the rail (DR-031).

    Deliberately the same rule as :attr:`SessionRecord.title` — the first thing
    the user asked for — extended to the prompt that is running right now. A
    session opened seconds ago has no stored record yet, and a rail row reading
    "(no requests yet)" while that prompt is visibly generating would name the
    one session the user is least likely to recognise.
    """
    prompts = [turn.prompt for turn in turns]
    if current and current.get("prompt"):
        prompts.append(str(current["prompt"]))
    for prompt in prompts:
        first = " ".join(prompt.split())
        if first:
            return first[:70] + ("…" if len(first) > 70 else "")
    return "New session"


def event_payload(event: Event) -> dict[str, Any]:
    """One logic event, passed through rather than interpreted.

    The dashboard decides what a ``tool_called`` *looks* like; what it means is
    already settled by the layer that emitted it.
    """
    return {
        "event": event.type,
        "message": event.message,
        "data": dict(event.data),
        "timestamp": event.timestamp,
    }


def tier_payloads() -> list[dict[str, Any]]:
    """The tier picker, derived from DR-003's single source of truth."""
    return [
        {
            "name": spec.name,
            "summary": spec.summary,
            "stages": list(spec.stages),
        }
        for spec in TIER_SPECS.values()
    ]


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n… ({len(value) - limit:,} more characters)"


def warnings_payload(*groups: Sequence[str] | Mapping[str, str]) -> list[str]:
    """Merge warning groups, keeping order and dropping exact repeats."""
    seen: list[str] = []
    for group in groups:
        for message in group:
            if message and message not in seen:
                seen.append(message)
    return seen
