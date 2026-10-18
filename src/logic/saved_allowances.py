"""Standing allowances that outlast one sitting (DR-057).

DR-048 made "Always allow" a standing yes for one session, held in memory:
reopening the session asked again, and there was no yes for a whole
workspace. This module keeps both, and keeps them so that a repository cannot
grant itself either:

* **Workspace rules** apply to every session in a workspace. They live in the
  user's config directory (``allowances.json``), keyed by the workspace's
  resolved path, never in the workspace: a file a clone or a pull can bring
  must not be able to say "run this without asking". The same reasoning as
  the watcher commands this machine trusts (DR-047).
* **A session's own rules** are saved with the session
  (``SessionRecord.allowances``). That record *is* in the workspace
  (``.autose/sessions/``), so the rules carry a seal: an HMAC of the
  workspace, the session id and the rules, made with a key kept in the same
  config file. A session whose rules do not match their seal — one that came
  in a clone, or was edited by hand — reopens with none of them.

Every rule read back, from either place, is checked again by today's rules
(:func:`recheck_rule`): one that would no longer be offered is dropped, not
trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .common.allowances import Allowance, allowance_for_rule
from .user_config import atomic_write_text, user_config_dir

__all__ = ["ALLOWANCES_FILENAME", "AllowanceFile", "recheck_rule"]

#: The file in the user's config directory.
ALLOWANCES_FILENAME = "allowances.json"

#: How many rules one workspace keeps; the oldest go first.
MAX_WORKSPACE_RULES = 200

#: One lock for every ``AllowanceFile`` in the process: the dashboard and each
#: session open the same file, and a read-modify-write must not interleave.
_LOCK = threading.Lock()

#: Bumped if what a seal covers changes, so an old seal stops opening.
_SEAL_VERSION = "autose-allowances-1"


def recheck_rule(tool: str, pattern: str) -> Allowance:
    """A rule read back from disk, held to today's rules (DR-057).

    The check a rule written by hand gets (DR-050), plus one: the rule must
    come back exactly as written. ``allowance_for_rule`` tidies what a person
    types (a URL becomes its host; a whole-tool rule ignores any pattern), and
    a stored rule that needs tidying was not written by AutoSE, so it is
    refused rather than reinterpreted. Raises ``ValueError`` with the reason.
    """
    allowance = allowance_for_rule(tool, pattern)
    if allowance.pattern != pattern:
        raise ValueError("it is not written the way AutoSE saves rules")
    return allowance


def _recheck_all(pairs: Iterable[tuple[str, str]]) -> tuple[tuple[Allowance, ...], tuple[str, ...]]:
    """The rules today's rules still allow, and a note for each one dropped."""
    kept: dict[str, Allowance] = {}
    dropped: list[str] = []
    for tool, pattern in pairs:
        try:
            allowance = recheck_rule(tool, pattern)
        except ValueError as error:
            dropped.append(f"{Allowance(tool, pattern).label}: {error}")
            continue
        kept.setdefault(allowance.key, allowance)
    return tuple(kept.values()), tuple(dropped)


class AllowanceFile:
    """Workspace rules, and the key that seals a session's rules (DR-057).

    Bound to one workspace, like ``TrustFile`` (DR-047). Every call reads the
    file again, so a rule added from the dashboard reaches a terminal that is
    open on the same workspace at its next question.
    """

    def __init__(self, path: Path, workspace: str | Path) -> None:
        self._path = Path(path)
        self._workspace = str(Path(workspace).resolve())
        #: Workspace rules read back and refused, with why, from the last read.
        self.problems: list[str] = []

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> AllowanceFile:
        """The file in the user's config directory, for *workspace*."""
        return cls(user_config_dir() / ALLOWANCES_FILENAME, workspace)

    @property
    def path(self) -> Path:
        return self._path

    # -- rules for every session in the workspace ---------------------------

    def rules(self) -> tuple[Allowance, ...]:
        """This workspace's rules that today's rules still allow."""
        with _LOCK:
            stored = self._read()["workspaces"].get(self._workspace, [])
        kept, dropped = _recheck_all(stored)
        self.problems = list(dropped)
        return kept

    def add(self, allowance: Allowance) -> tuple[Allowance, ...]:
        """Let *allowance* run unasked in every session here.

        Checked again first, so nothing reaches the file that would be
        refused on the way back. Raises ``ValueError`` for a refused rule and
        ``OSError`` when the file cannot be written.
        """
        recheck_rule(allowance.tool, allowance.pattern)
        with _LOCK:
            document = self._read()
            rules = [
                pair
                for pair in document["workspaces"].get(self._workspace, [])
                if f"{pair[0]}:{pair[1]}" != allowance.key
            ]
            rules.append((allowance.tool, allowance.pattern))
            document["workspaces"][self._workspace] = rules[-MAX_WORKSPACE_RULES:]
            self._write(document)
        return self.rules()

    def remove(self, key: str) -> bool:
        """Take one workspace rule back. Returns whether it was there."""
        with _LOCK:
            document = self._read()
            rules = document["workspaces"].get(self._workspace, [])
            kept = [pair for pair in rules if f"{pair[0]}:{pair[1]}" != key]
            if len(kept) == len(rules):
                return False
            if kept:
                document["workspaces"][self._workspace] = kept
            else:
                del document["workspaces"][self._workspace]
            self._write(document)
        return True

    # -- a session's own rules, sealed ----------------------------------------

    def seal(self, session_id: str, allowances: Iterable[Allowance]) -> str:
        """A seal over *allowances* for one session in this workspace.

        Makes this machine's key on first use. Empty when the key cannot be
        written: the rules are then saved without a seal, and reopening the
        session asks again rather than trusting them.
        """
        keys = [item.key for item in allowances]
        if not keys:
            return ""
        with _LOCK:
            document = self._read()
            key = document["key"]
            if not key:
                key = secrets.token_hex(32)
                document["key"] = key
                try:
                    self._write(document)
                except OSError:
                    return ""
        return _mac(key, self._workspace, session_id, keys)

    def restore(
        self, session_id: str, pairs: Iterable[tuple[str, str]], seal: str
    ) -> tuple[tuple[Allowance, ...], tuple[str, ...]]:
        """A saved session's rules, if its seal opens, re-checked one by one.

        Returns the rules kept and a note for whatever was dropped.
        """
        pairs = [(str(tool), str(pattern)) for tool, pattern in pairs]
        if not pairs:
            return (), ()
        with _LOCK:
            key = self._read()["key"]
        expected = (
            _mac(key, self._workspace, session_id, [f"{t}:{p}" for t, p in pairs])
            if key
            else ""
        )
        if not seal or not expected or not hmac.compare_digest(expected, seal):
            return (), (
                f"{len(pairs)} saved rule(s) were not restored: they were not saved "
                "for this folder by AutoSE on this computer",
            )
        return _recheck_all(pairs)

    # -- the file ---------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        """The document, with every part this module relies on in shape.

        An unreadable file reads as empty. The next write replaces it, which
        loses rules and the key: a fail-closed outcome, since sealed sessions
        then reopen asking again.
        """
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if not isinstance(loaded, dict):
            loaded = {}
        key = loaded.get("key")
        workspaces: dict[str, list[tuple[str, str]]] = {}
        stored = loaded.get("workspaces")
        if isinstance(stored, dict):
            for path, rules in stored.items():
                if not isinstance(rules, list):
                    continue
                workspaces[str(path)] = [
                    (str(rule.get("tool", "")), str(rule.get("pattern", "")))
                    for rule in rules
                    if isinstance(rule, dict)
                ]
        return {
            "key": key if isinstance(key, str) and _is_hex(key) else "",
            "workspaces": workspaces,
        }

    def _write(self, document: dict[str, Any]) -> None:
        payload = {
            "version": 1,
            "key": document["key"],
            "workspaces": {
                path: [{"tool": tool, "pattern": pattern} for tool, pattern in rules]
                for path, rules in document["workspaces"].items()
            },
        }
        atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=1))


def _mac(key: str, workspace: str, session_id: str, keys: Iterable[str]) -> str:
    message = json.dumps([_SEAL_VERSION, workspace, session_id, sorted(set(keys))])
    return hmac.new(bytes.fromhex(key), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _is_hex(text: str) -> bool:
    try:
        return len(bytes.fromhex(text)) >= 16
    except ValueError:
        return False
