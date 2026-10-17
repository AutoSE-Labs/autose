"""The validated, batch single-file edit tool (DR-007).

One call carries a target path plus a batch of ``{old_str, new_str}`` pairs —
at most 20 by default, any number where a repo configures ``-1`` (DR-030). The
guarantees DR-007 requires, and what each one buys:

* **Every ``old_str`` must match exactly once against the *original* file.**
  Validating against the original — not against the partially-rewritten
  intermediate — is what makes the result independent of the order the model
  happened to list the edits in.
* **Any problem rejects the whole batch**, leaving the file byte-for-byte
  unchanged. There is no partial application to reason about or clean up.
* **Replacements apply back-to-front** in a single pass, so earlier offsets
  stay valid without bookkeeping.
* **Encoding characteristics survive**: a UTF-8 BOM is preserved, and a
  consistently-CRLF file stays CRLF. A file with mixed endings gets plain
  exact matching and no normalisation, since there is no consistent
  convention to preserve.

There is deliberately no fuzzy matching. A stale read should fail loudly here
rather than land a plausible-looking edit in the wrong place.
"""

from __future__ import annotations

import codecs
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from ...errors import WorkspaceEscapeError
from ...limits import count_limit
from ..workspace import Workspace

__all__ = ["edit_file", "MAX_EDITS_PER_CALL"]

#: Upper bound on replacement pairs in one call (DR-007). ``-1`` accepts a
#: batch of any size (DR-030) — the whole batch is still validated against the
#: original file and still applied atomically, so a larger one is not a weaker
#: guarantee, only a bigger call to get wrong in one go.
MAX_EDITS_PER_CALL = 20


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    new: str
    index: int


def edit_file(
    path: str,
    edits: Sequence[Any] | None = None,
    *,
    workspace: Workspace,
    max_edits_per_call: int | None = MAX_EDITS_PER_CALL,
) -> str:
    """Apply a validated batch of exact replacements to one file."""
    try:
        target = workspace.resolve(path)
    except WorkspaceEscapeError as exc:
        return f"Error: {exc}"
    if not target.exists():
        return f"Error: file not found: {path}"
    if not target.is_file():
        return f"Error: not a file: {path}"

    problem = _validate_batch_shape(edits, max_edits_per_call)
    if problem:
        return problem
    assert edits is not None

    raw = target.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    body = raw[len(codecs.BOM_UTF8) :] if has_bom else raw
    try:
        original = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: file is not valid UTF-8 text: {path}"

    crlf_only = _is_consistently_crlf(original)
    content = original.replace("\r\n", "\n") if crlf_only else original

    planned: list[_Replacement] = []
    for index, edit in enumerate(edits):
        outcome = _plan_replacement(edit, index, content, crlf_only, path)
        if isinstance(outcome, str):
            return outcome
        planned.append(outcome)

    ordered = sorted(planned, key=lambda item: item.start)
    for previous, current in pairwise(ordered):
        if current.start < previous.end:
            return (
                f"Error: edits[{previous.index}] and edits[{current.index}] target "
                "overlapping ranges; merge them into one replacement."
            )

    updated = content
    for replacement in reversed(ordered):
        updated = (
            updated[: replacement.start] + replacement.new + updated[replacement.end :]
        )
    if crlf_only:
        updated = updated.replace("\n", "\r\n")

    _write_once(target, updated, has_bom=has_bom)
    return f"Edited {len(planned)} block(s) in {workspace.display(target)}"


# -- validation ---------------------------------------------------------


def _validate_batch_shape(
    edits: Sequence[Any] | None, max_edits_per_call: int | None = MAX_EDITS_PER_CALL
) -> str | None:
    if edits is None:
        return (
            "Error: 'edits' is required — pass an array of "
            "{old_str, new_str} objects, even for a single replacement."
        )
    if isinstance(edits, (str, bytes)) or not isinstance(edits, Sequence):
        return "Error: 'edits' must be an array of {old_str, new_str} objects."
    if not edits:
        return "Error: 'edits' must contain at least one replacement."
    limit = count_limit(max_edits_per_call)
    if limit is not None and len(edits) > limit:
        return (
            f"Error: 'edits' may contain at most {limit} replacements; "
            f"got {len(edits)}. Split the change across calls."
        )
    return None


def _plan_replacement(
    edit: Any, index: int, content: str, crlf_only: bool, path: str
) -> _Replacement | str:
    if not isinstance(edit, dict):
        return f"Error: edits[{index}] must be an object with old_str and new_str."
    old = edit.get("old_str")
    new = edit.get("new_str")
    if not isinstance(old, str) or not isinstance(new, str):
        return f"Error: edits[{index}] requires string 'old_str' and 'new_str'."
    if crlf_only:
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
    if not old:
        return f"Error: edits[{index}].old_str cannot be empty."
    if old == new:
        return f"Error: edits[{index}] would make no change."

    count = content.count(old)
    if count == 0:
        return (
            f"Error: edits[{index}].old_str was not found in {path}. "
            "Re-read the file — it may have changed since you last read it."
        )
    if count > 1:
        return (
            f"Error: edits[{index}].old_str appears {count} times in {path}; "
            "include more surrounding context to make it unique."
        )
    start = content.index(old)
    return _Replacement(start=start, end=start + len(old), new=new, index=index)


def _is_consistently_crlf(text: str) -> bool:
    """True when every line ending in *text* is CRLF (and there is at least one)."""
    return "\r\n" in text and "\n" not in text.replace("\r\n", "")


def _write_once(target: Path, content: str, *, has_bom: bool) -> None:
    prefix = codecs.BOM_UTF8 if has_bom else b""
    target.write_bytes(prefix + content.encode("utf-8"))
