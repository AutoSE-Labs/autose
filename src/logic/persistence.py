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
exactly as it applies to the live one.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .common.changes import FileChange

__all__ = [
    "SESSION_FORMAT_VERSION",
    "SessionRecord",
    "TurnRecord",
    "SessionStore",
    "SessionStoreError",
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

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "answer": self.answer,
            "tier": self.tier,
            "stop_reason": self.stop_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "files_changed": list(self.files_changed),
            "changes": [_change_to_dict(change) for change in self.changes],
            "requirements": [dict(item) for item in self.requirements],
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> TurnRecord:
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
            requirements=tuple(
                {key: str(item.get(key, "")) for key in _REQUIREMENT_KEYS}
                for item in document.get("requirements", ())
                if isinstance(item, Mapping)
            ),
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
    turns: tuple[TurnRecord, ...] = ()
    #: `SessionMemory`'s rolling compressed summary, and the exchanges it was
    #: still holding verbatim (DR-004).
    summary: str = ""
    verbatim: tuple[tuple[str, str], ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def title(self) -> str:
        """A one-line label: the first thing the user asked for."""
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
            "summary": self.summary,
            "verbatim": [list(pair) for pair in self.verbatim],
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "turns": [turn.to_dict() for turn in self.turns],
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
        return cls(
            id=identifier,
            workspace=str(document.get("workspace", "")),
            created_at=_float(document.get("created_at")),
            updated_at=_float(document.get("updated_at")),
            provider=str(document.get("provider", "")),
            base_url=str(document.get("base_url", "")),
            model=str(document.get("model", "")),
            tier=str(document.get("tier", "")),
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
        )

    def bounded(
        self,
        *,
        max_change_bytes: int = DEFAULT_MAX_STORED_CHANGE_BYTES,
        max_session_bytes: int = DEFAULT_MAX_STORED_SESSION_BYTES,
    ) -> SessionRecord:
        """Return a copy whose stored change content fits the caps.

        A change past a cap keeps its path and its place in the history and
        loses its content, marked with the reason — so a resumed session says
        "this file changed, and I can no longer put it back" rather than
        either lying about it or holding a 40 MB blob in a JSON file.
        """
        budget = max_session_bytes
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
            turns.append(replace(turn, changes=tuple(trimmed)))
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


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
