"""Durable session state: what a session was, so it can be picked up again (DR-026).

A ``Session`` held everything it knew in memory — the turns, the rolling
history, the ratcheted tier, the token tally, and (since DR-024) the content
needed to undo what it wrote. Closing the terminal discarded all of it. On the
hardware DR-015 targets a single `craft` request can run for an hour, so
"start again from the beginning" is an expensive answer to a closed laptop
lid.

This module is the storage half. It owns:

* :class:`SessionRecord` — one session as plain data, with an explicit format
  version, so a stored session written by an older AutoSE is *recognised as
  old* rather than silently misread.
* :class:`SessionStore` — the Strategy seam. A second backend (a database, a
  synced directory) is one implementation, with no edit to `Session`.
* :class:`FileSessionStore` — one JSON file per session under
  ``.autose/sessions/``, which is already the git-ignored directory DR-011's
  tool-output artifacts live in.

Two rules shape everything here:

**Persistence never breaks a run.** Every failure path — an unwritable
directory, a truncated file, a JSON document from the future — degrades to
"this session is not saved" or "this session cannot be resumed", and is
reported. A session that works is never sacrificed to a session that might be
resumed.

**Only what a session promoted is stored.** The record holds the same distilled
`(prompt, answer)` pairs `SessionMemory` holds, never raw tool transcripts or
intermediate stage artifacts — DR-004's boundary applies to the durable copy
exactly as it applies to the live one. A turn's step trace (DR-055) is a
bounded record of what it did for a person to read back: trimmed arguments and
result previews rather than the transcript itself, and never read into a prompt.
"""

from __future__ import annotations

import base64
import json
import math
import os
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .common.changes import FileChange
from .common.trace import RunTrace

__all__ = [
    "SESSION_FORMAT_VERSION",
    "SessionRecord",
    "TurnRecord",
    "SessionStore",
    "SessionStoreError",
    "QueuedRequest",
    "FileSessionStore",
    "NullSessionStore",
    "SESSIONS_DIRECTORY",
    "new_session_id",
]

#: Bumped whenever the stored shape changes incompatibly. A record naming a
#: version this build does not understand is refused with an explanation
#: rather than parsed optimistically.
SESSION_FORMAT_VERSION = 1

#: Where sessions live, inside the git-ignored directory AutoSE already owns.
SESSIONS_DIRECTORY = Path(".autose") / "sessions"

#: Largest single file's before/after content carried into storage. Past this
#: the change is still recorded — the path, the fact it changed — but its
#: content is dropped and it is marked unrestorable, matching DR-024's rule
#: that a bound being hit is reported rather than hidden.
DEFAULT_MAX_STORED_CHANGE_BYTES = 256 * 1024

#: Ceiling on stored change content for one whole session.
DEFAULT_MAX_STORED_SESSION_BYTES = 8 * 1024 * 1024

#: Ceiling on stored step traces for one whole session (DR-055). Each trace
#: is already capped per turn; this keeps a long session's file from growing
#: by a trace per run without end. Past it, older turns keep their stage
#: times and a count of what was there, and lose the steps themselves.
DEFAULT_MAX_STORED_TRACE_BYTES = 2 * 1024 * 1024

#: How many sessions a store keeps before pruning the oldest.
DEFAULT_MAX_SESSIONS = 50


class SessionStoreError(Exception):
    """A stored session could not be read or written."""


def new_session_id() -> str:
    """A sortable, collision-resistant identifier.

    Time-ordered so a directory listing is chronological and "the latest
    session" needs no index, with a random suffix so two sessions started in
    the same second do not overwrite each other.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


#: The fields a stored requirement keeps (DR-038).
_REQUIREMENT_KEYS = ("id", "status", "description", "evidence")

#: The fields a stored route step keeps (see ``WorkflowOutcome.route``): the
#: shape of the run and which requirements sent it back, not the validator's
#: feedback text, which is intermediate reasoning DR-004 keeps off disk like
#: the stage artifacts.
_ROUTE_KEYS = ("kind", "stage", "title", "unmet")


@dataclass(frozen=True)
class TurnRecord:
    """One completed exchange, in the form that goes to disk."""

    prompt: str
    answer: str
    tier: str
    stop_reason: str = "completed"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    files_changed: tuple[str, ...] = ()
    changes: tuple[FileChange, ...] = ()
    #: The final requirements ledger (DR-038). Optional on read: a session
    #: written before it existed simply has none, which is not a format change.
    requirements: tuple[dict[str, str], ...] = ()
    #: The stages and loop-backs, in order. Optional on read, like the ledger.
    route: tuple[dict[str, str], ...] = ()
    #: Changes an undo reverted, kept for the record. Optional on read.
    undone: tuple[FileChange, ...] = ()
    #: What the turn did step by step (DR-055). Optional on read: a turn
    #: stored before traces were kept has none, which is not a format change.
    trace: RunTrace | None = None

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "completed"

    def to_dict(self) -> dict[str, Any]:
        document = {
            "prompt": self.prompt,
            "answer": self.answer,
            "tier": self.tier,
            "stop_reason": self.stop_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "files_changed": list(self.files_changed),
            "changes": [_change_to_dict(change) for change in self.changes],
            "undone": [_change_to_dict(change) for change in self.undone],
            "requirements": [dict(item) for item in self.requirements],
            "route": [
                {key: item[key] for key in _ROUTE_KEYS if key in item}
                for item in self.route
            ],
        }
        if self.trace is not None:
            document["trace"] = self.trace.to_dict()
        return document

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> TurnRecord:
        trace = document.get("trace")
        return cls(
            prompt=str(document.get("prompt", "")),
            answer=str(document.get("answer", "")),
            tier=str(document.get("tier", "")),
            stop_reason=str(document.get("stop_reason", "completed")),
            prompt_tokens=_int(document.get("prompt_tokens")),
            completion_tokens=_int(document.get("completion_tokens")),
            files_changed=tuple(str(p) for p in document.get("files_changed", ())),
            changes=tuple(
                _change_from_dict(item) for item in document.get("changes", ())
            ),
            undone=tuple(
                _change_from_dict(item) for item in document.get("undone", ())
            ),
            requirements=tuple(
                {key: str(item.get(key, "")) for key in _REQUIREMENT_KEYS}
                for item in document.get("requirements", ())
                if isinstance(item, Mapping)
            ),
            route=tuple(
                {key: str(item.get(key, "")) for key in _ROUTE_KEYS if key in item}
                for item in document.get("route", ())
                if isinstance(item, Mapping)
            ),
            trace=RunTrace.from_dict(trace) if isinstance(trace, Mapping) else None,
        )


@dataclass(frozen=True)
class QueuedRequest:
    """A request waiting to run in a session, as it goes to disk (DR-056).

    What the person typed and the tier they chose, nothing the session made of
    it: a queued request has not run, so there is no answer or change to keep,
    and storing the person's own words crosses no boundary DR-004 draws. The id
    is the one the Adapter gave it, kept so a reopened queue's items are still
    moved and removed by the same handle.
    """

    id: str
    prompt: str
    tier: str = ""
    queued_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "tier": self.tier,
            "queued_at": self.queued_at,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> QueuedRequest:
        return cls(
            id=str(document.get("id", "")),
            prompt=str(document.get("prompt", "")),
            tier=str(document.get("tier", "")),
            queued_at=_float(document.get("queued_at")),
        )


@dataclass(frozen=True)
class SessionRecord:
    """One session, as plain data."""

    id: str
    workspace: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    provider: str = ""
    base_url: str = ""
    model: str = ""
    #: The tier the session had ratcheted to (DR-003).
    tier: str = ""
    #: A name the person gave it; the title falls back to the first request.
    name: str = ""
    #: Requests waiting to run, in order (DR-056). Optional on read: a session
    #: written before queues were kept simply has none.
    queue: tuple[QueuedRequest, ...] = ()
    turns: tuple[TurnRecord, ...] = ()
    #: `SessionMemory`'s rolling compressed summary, and the exchanges it was
    #: still holding verbatim (DR-004).
    summary: str = ""
    verbatim: tuple[tuple[str, str], ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: The session's own "always allow" rules as ``(tool, pattern)`` pairs,
    #: and the seal that shows AutoSE wrote them on this machine (DR-057).
    #: Optional on read: a session saved before they were kept has none.
    allowances: tuple[tuple[str, str], ...] = ()
    allowances_seal: str = ""

    @property
    def title(self) -> str:
        """A one-line label: its name, else the first thing the user asked for."""
        if self.name.strip():
            return self.name.strip()
        for turn in self.turns:
            first = " ".join(turn.prompt.split())
            if first:
                return first[:70] + ("…" if len(first) > 70 else "")
        return "(no requests yet)"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def files_changed(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for turn in self.turns:
            for path in turn.files_changed:
                seen.setdefault(path, None)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SESSION_FORMAT_VERSION,
            "id": self.id,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "tier": self.tier,
            "name": self.name,
            "queue": [item.to_dict() for item in self.queue],
            "summary": self.summary,
            "verbatim": [list(pair) for pair in self.verbatim],
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "turns": [turn.to_dict() for turn in self.turns],
            "allowances": [
                {"tool": tool, "pattern": pattern} for tool, pattern in self.allowances
            ],
            "allowances_seal": self.allowances_seal,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> SessionRecord:
        version = _int(document.get("version"))
        if version > SESSION_FORMAT_VERSION:
            raise SessionStoreError(
                f"this session was written by a newer AutoSE (format {version}; "
                f"this build understands {SESSION_FORMAT_VERSION}). Upgrade, or "
                "start a new session."
            )
        identifier = str(document.get("id") or "")
        if not identifier:
            raise SessionStoreError("stored session has no id")
        # DR-056: a hand-edited file may hold anything here; only a list is a
        # queue, and anything else is read as none rather than failing the
        # whole session list.
        waiting = document.get("queue")
        if not isinstance(waiting, list):
            waiting = []
        return cls(
            id=identifier,
            workspace=str(document.get("workspace", "")),
            created_at=_float(document.get("created_at")),
            updated_at=_float(document.get("updated_at")),
            provider=str(document.get("provider", "")),
            base_url=str(document.get("base_url", "")),
            model=str(document.get("model", "")),
            tier=str(document.get("tier", "")),
            name=str(document.get("name", "")),
            queue=tuple(
                QueuedRequest.from_dict(item)
                for item in waiting
                if isinstance(item, Mapping) and str(item.get("prompt", "")).strip()
            ),
            summary=str(document.get("summary", "")),
            verbatim=tuple(
                (str(pair[0]), str(pair[1]))
                for pair in document.get("verbatim", ())
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ),
            prompt_tokens=_int(document.get("prompt_tokens")),
            completion_tokens=_int(document.get("completion_tokens")),
            turns=tuple(
                TurnRecord.from_dict(item) for item in document.get("turns", ())
            ),
            allowances=tuple(
                (str(item.get("tool", "")), str(item.get("pattern", "")))
                for item in _list(document.get("allowances"))
                if isinstance(item, Mapping)
            ),
            allowances_seal=str(document.get("allowances_seal", "")),
        )

    def bounded(
        self,
        *,
        max_change_bytes: int = DEFAULT_MAX_STORED_CHANGE_BYTES,
        max_session_bytes: int = DEFAULT_MAX_STORED_SESSION_BYTES,
        max_trace_bytes: int = DEFAULT_MAX_STORED_TRACE_BYTES,
    ) -> SessionRecord:
        """Return a copy whose stored change content and traces fit the caps.

        A change past a cap keeps its path and its place in the history and
        loses its content, marked with the reason — so a resumed session says
        "this file changed, and I can no longer put it back" rather than
        either lying about it or holding a 40 MB blob in a JSON file. A trace
        past the session's trace budget keeps its stage times and says how
        many steps and rounds it no longer holds (DR-055).
        """
        budget = max_session_bytes
        trace_budget = max_trace_bytes
        turns: list[TurnRecord] = []
        # Newest first: if the session budget runs out, the recent turns —
        # the ones a user might actually undo — are the ones that keep their
        # content.
        for turn in reversed(self.turns):
            trimmed: list[FileChange] = []
            for change in turn.changes:
                size = len(change.before or b"") + len(change.after or b"")
                if not change.restorable or size > max_change_bytes or size > budget:
                    trimmed.append(
                        replace(
                            change,
                            before=None,
                            after=None,
                            restorable=False,
                            reason=change.reason
                            or "its contents were too large to store with the session",
                        )
                    )
                    continue
                budget -= size
                trimmed.append(change)
            trace = turn.trace
            if trace is not None and trace.has_steps:
                # The same newest-first rule: the runs a person is most likely
                # to look back at are the recent ones.
                size = _trace_size(trace)
                if size > trace_budget:
                    trace = trace.without_detail()
                else:
                    trace_budget -= size
            turns.append(replace(turn, changes=tuple(trimmed), trace=trace))
        return replace(self, turns=tuple(reversed(turns)))


@runtime_checkable
class SessionStore(Protocol):
    """Strategy seam: where sessions are kept."""

    def save(self, record: SessionRecord) -> None:
        """Write *record*, replacing any earlier copy of the same session."""

    def load(self, session_id: str) -> SessionRecord | None:
        """Read one session, or ``None`` when it is not there."""

    def list(self, limit: int = 0) -> tuple[SessionRecord, ...]:
        """Every stored session, newest first."""

    def latest(self) -> SessionRecord | None:
        """The most recently updated session, or ``None``."""

    def delete(self, session_id: str) -> bool:
        """Remove one session. Returns whether it existed."""


class NullSessionStore:
    """Keeps nothing. The default when a caller asked for no persistence."""

    def save(self, record: SessionRecord) -> None:  # Protocol implementation.
        return None

    def load(self, session_id: str) -> SessionRecord | None:  # Protocol implementation.
        return None

    def list(self, limit: int = 0) -> tuple[SessionRecord, ...]:  # Protocol implementation.
        return ()

    def latest(self) -> SessionRecord | None:  # Protocol implementation.
        return None

    def delete(self, session_id: str) -> bool:  # Protocol implementation.
        return False


class FileSessionStore:
    """One JSON file per session, under the workspace's ``.autose/sessions/``."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_change_bytes: int = DEFAULT_MAX_STORED_CHANGE_BYTES,
        max_session_bytes: int = DEFAULT_MAX_STORED_SESSION_BYTES,
    ) -> None:
        self._root = Path(root)
        self._max_sessions = max(1, max_sessions)
        self._max_change_bytes = max_change_bytes
        self._max_session_bytes = max_session_bytes
        #: Reasons a save or load degraded, for an Adapter to surface. Never
        #: raised at the caller: a broken store must not break a run.
        self.problems: list[str] = []

    @property
    def root(self) -> Path:
        return self._root

    # -- writing -----------------------------------------------------------

    def save(self, record: SessionRecord) -> None:
        """Write the session, atomically, and prune what is past the cap."""
        bounded = record.bounded(
            max_change_bytes=self._max_change_bytes,
            max_session_bytes=self._max_session_bytes,
        )
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._write_gitignore()
            target = self._path(bounded.id)
            # Write beside the target and rename, so an interrupted save
            # leaves the previous session intact rather than a half file.
            temporary = target.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(bounded.to_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except (OSError, TypeError, ValueError) as exc:
            self._note(f"could not save this session: {exc}")
            return
        self._prune()

    # -- reading -----------------------------------------------------------

    def load(self, session_id: str) -> SessionRecord | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        return self._read(path)

    def list(self, limit: int = 0) -> tuple[SessionRecord, ...]:
        records = [
            record
            for record in (self._read(path) for path in self._files())
            if record is not None
        ]
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return tuple(records[:limit] if limit > 0 else records)

    def latest(self) -> SessionRecord | None:
        found = self.list(limit=1)
        return found[0] if found else None

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def resolve(self, reference: str) -> SessionRecord | None:
        """Find a session by exact id, or by an unambiguous id prefix."""
        exact = self.load(reference)
        if exact is not None:
            return exact
        matches = [
            record for record in self.list() if record.id.startswith(reference)
        ]
        return matches[0] if len(matches) == 1 else None

    # -- internals -----------------------------------------------------------

    def _files(self) -> Iterable[Path]:
        try:
            return sorted(self._root.glob("*.json"))
        except OSError:
            return ()

    def _path(self, session_id: str) -> Path:
        return self._root / f"{_safe_id(session_id)}.json"

    def _read(self, path: Path) -> SessionRecord | None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._note(f"{path.name} could not be read ({exc}); skipping it")
            return None
        if not isinstance(document, dict):
            self._note(f"{path.name} is not a stored session; skipping it")
            return None
        try:
            return SessionRecord.from_dict(document)
        except SessionStoreError as exc:
            self._note(f"{path.name}: {exc}")
            return None

    def _prune(self) -> None:
        """Drop the oldest sessions past the cap. Best effort, never fatal."""
        try:
            paths = sorted(
                self._files(), key=lambda path: path.stat().st_mtime, reverse=True
            )
        except OSError:
            return
        for path in paths[self._max_sessions :]:
            try:
                path.unlink()
            except OSError:
                continue

    def _write_gitignore(self) -> None:
        """Keep stored sessions out of the user's history, best effort.

        The same posture as DR-011's artifact store: a transcript of someone's
        work is theirs, and it must not arrive in a commit because AutoSE
        wrote it into a tracked directory.
        """
        marker = self._root / ".gitignore"
        if marker.exists():
            return
        try:
            marker.write_text("*\n", encoding="utf-8")
        except OSError:
            return

    def _note(self, problem: str) -> None:
        self.problems.append(problem)
        del self.problems[:-10]


# -- serialisation helpers -----------------------------------------------------


def _change_to_dict(change: FileChange) -> dict[str, Any]:
    return {
        "path": change.path,
        "before": _encode(change.before),
        "after": _encode(change.after),
        "tool": change.tool,
        "restorable": change.restorable,
        "reason": change.reason,
    }


def _change_from_dict(document: Any) -> FileChange:
    if not isinstance(document, Mapping):
        return FileChange(path="", restorable=False, reason="unreadable record")
    return FileChange(
        path=str(document.get("path", "")),
        before=_decode(document.get("before")),
        after=_decode(document.get("after")),
        tool=str(document.get("tool", "")),
        restorable=bool(document.get("restorable", True)),
        reason=str(document.get("reason", "")),
    )


#: How deep a turn's trace sits in a session file (the record, its turns, the
#: turn): each of the trace's lines is indented this much more there.
_TRACE_DEPTH = 3


def _trace_size(trace: RunTrace) -> int:
    """What a trace costs in the session file, in bytes.

    Measured as :meth:`FileSessionStore.save` writes it — UTF-8, indented —
    not as compact JSON in characters, which undercounts the written file by
    a third or more and by up to four times for text that is not ASCII.
    """
    text = json.dumps(trace.to_dict(), ensure_ascii=False, indent=1)
    return len(text.encode("utf-8")) + _TRACE_DEPTH * text.count("\n")


def _encode(content: bytes | None) -> str | None:
    """Base64 rather than text: a change may hold bytes that are not UTF-8."""
    return None if content is None else base64.b64encode(content).decode("ascii")


def _decode(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None


def _safe_id(session_id: str) -> str:
    """Reduce an id to characters that cannot escape the sessions directory."""
    cleaned = "".join(
        character
        for character in session_id
        if character.isalnum() or character in "-_"
    )
    return cleaned or "unnamed"


def _list(value: Any) -> list[Any]:
    """*value* when it is a list, else empty: a hand-edited field reads as none."""
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: JSON reads 1e999 as infinity, which has no int.
        return 0


def _float(value: Any) -> float:
    # A hand-edited file can hold NaN or Infinity, which Python's json reads
    # but the page cannot: one such time in a payload stops it loading.
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0
