"""Change a few scalar settings in YAML text without losing its comments.

PyYAML reads and writes YAML but does not round-trip it: ``safe_load`` then
``safe_dump`` drops every comment, blank line and choice of quoting. That is
fine for a file AutoSE owns and wrong for one a person wrote, such as their
``config.yaml`` or a repository's committed ``.autose/config.yaml`` (DR-021),
where the comments are often the documentation (DR-059).

:func:`set_scalars` edits the text instead, a line at a time, for the narrow
shape configuration files have: block mappings, one ``key: value`` per line,
consistent indentation. It

- changes a value on its own line, keeping the indentation, the quoting style
  and any trailing comment;
- adds a missing key at the end of its parent's block;
- adds a missing parent block, at the end of its own parent.

Anything it is not sure of on the way to a key (flow style such as
``{a: 1}``, anchors, aliases, tags, block scalars, a nested value where a
scalar is being set, duplicate keys, several documents, tab indentation,
quoted keys) makes it return ``None``, so the caller can fall back to a full
rewrite. Even a result is not trusted on its own:
:func:`logic.user_config.update_yaml_scalars` parses it and keeps it only if
it means exactly what a load-update-dump would have written.

No I/O, and nothing but PyYAML, which is used to quote values exactly as YAML
would.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

__all__ = ["Scalar", "set_scalars"]

#: What a setting can be set to.
Scalar = str | int | float | bool | None

#: One plain key, as configuration files spell them.
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")

#: A block-mapping entry: indentation, a plain key, the colon, and the rest of
#: the line (value and comment). The colon must be followed by a space or the
#: end of the line, or it is part of a plain scalar such as a URL.
_ENTRY = re.compile(r"(?P<indent> *)(?P<key>[A-Za-z0-9_][A-Za-z0-9_.-]*) *:(?= |$)(?P<rest>.*)")

#: Value openers this editor does not touch: flow collections, anchors,
#: aliases, tags, block scalars, and the reserved indicators.
_UNSUPPORTED_OPENERS = frozenset("[{&*!|>%@`")

#: Indentation for a new block when the file shows none to copy.
_DEFAULT_STEP = 2


class _Line:
    """One line of the text: its content and its line ending, kept apart."""

    __slots__ = ("body", "end")

    def __init__(self, body: str, end: str) -> None:
        self.body = body
        self.end = end

    @property
    def structural(self) -> bool:
        """Whether YAML reads anything here: not blank and not a comment."""
        stripped = self.body.strip()
        return bool(stripped) and not stripped.startswith("#")

    @property
    def indent(self) -> int:
        return len(self.body) - len(self.body.lstrip(" "))


def set_scalars(text: str, updates: Mapping[str, Scalar]) -> str | None:
    """*text* with each dotted key in *updates* set, or ``None`` when unsure.

    ``{"inference.model": "qwen3:8b"}`` sets ``model`` under ``inference``,
    creating either if missing. Keys are applied in order; the line endings of
    the text are kept, and new lines use the ones it already has.
    """
    lines = _split(text)
    if lines is None:
        return None
    newline = "\r\n" if "\r\n" in text else "\n"
    for dotted, value in updates.items():
        path = dotted.split(".")
        if not all(_NAME.fullmatch(part) for part in path):
            return None
        rendered = _render(value)
        if rendered is None or not _set(lines, path, value, rendered, newline):
            return None
    return "".join(line.body + line.end for line in lines)


# -- reading the text ---------------------------------------------------------------


def _split(text: str) -> list[_Line] | None:
    """The text as lines, or ``None`` when it is not a shape edited here."""
    lines: list[_Line] = []
    for piece in re.split(r"(?<=\n)", text):
        if not piece:
            continue
        body = piece.rstrip("\r\n")
        lines.append(_Line(body, piece[len(body):]))
    seen_content = False
    for line in lines:
        if not line.structural:
            continue
        leading = line.body[: len(line.body) - len(line.body.lstrip())]
        if "\t" in leading or line.body.startswith("%"):
            # Tabs are not YAML indentation; `%` starts a directive.
            return None
        marker = line.body.rstrip()
        if marker == "---" and not seen_content:
            # A single document marker at the top is common and harmless.
            seen_content = True
            continue
        if marker.startswith(("---", "...")):
            # A second document, a document end, or content on the marker line.
            return None
        seen_content = True
    return lines


def _content_indent(lines: list[_Line], start: int, end: int) -> int | None:
    """Indentation of the first line YAML reads in ``[start, end)``."""
    for line in lines[start:end]:
        if line.structural and line.body.rstrip() != "---":
            return line.indent
    return None


def _block_end(lines: list[_Line], key_line: int, end: int) -> int:
    """Where the value of the key on *key_line* stops: the next line at its level."""
    indent = lines[key_line].indent
    for index in range(key_line + 1, end):
        line = lines[index]
        if line.structural and line.indent <= indent:
            return index
    return end


def _last_content(lines: list[_Line], start: int, end: int) -> int | None:
    """The last line YAML reads in ``[start, end)``, so new keys go after it."""
    for index in range(end - 1, start - 1, -1):
        if lines[index].structural:
            return index
    return None


def _step(lines: list[_Line]) -> int:
    """The file's own indentation step: its smallest indentation of content."""
    indents = [line.indent for line in lines if line.structural and line.indent > 0]
    return min(indents) if indents else _DEFAULT_STEP


# -- editing ------------------------------------------------------------------------


def _set(
    lines: list[_Line], path: list[str], value: Scalar, rendered: str, newline: str
) -> bool:
    """Set one dotted key in place. ``False`` means the caller must not trust the text."""
    start, end = 0, len(lines)
    parent_indent = -1
    parent_line: int | None = None
    for depth, key in enumerate(path):
        level = _content_indent(lines, start, end)
        if level is not None and level <= parent_indent:
            return False
        sure, index = _find(lines, start, end, level, key)
        if not sure:
            return False
        if index is None:
            if level is not None:
                indent = level
            elif parent_indent >= 0:
                indent = parent_indent + _step(lines)
            else:
                indent = 0
            _insert(lines, start, end, parent_line, indent, path[depth:], rendered, newline)
            return True
        match = _ENTRY.fullmatch(lines[index].body)
        assert match is not None  # _find only returns lines that match
        block_end = _block_end(lines, index, end)
        if depth == len(path) - 1:
            if _content_indent(lines, index + 1, block_end) is not None:
                # A nested block, or a value continued on the next lines.
                return False
            return _replace_value(lines[index], match, value, rendered)
        parts = _split_value(match.group("rest"))
        if parts is None or parts[0]:
            # `inference: {…}`, `inference: &anchor`, or a scalar: not a block.
            return False
        start, end = index + 1, block_end
        parent_indent = len(match.group("indent"))
        parent_line = index
    return False  # pragma: no cover - the loop always returns


def _find(
    lines: list[_Line], start: int, end: int, level: int | None, key: str
) -> tuple[bool, int | None]:
    """``(sure, line)``: the line that holds *key* at this level, ``None`` if absent.

    Not sure means the level is not a plain block mapping (a sequence, a flow
    collection, a quoted or complex key) or it names *key* twice.
    """
    if level is None:
        return True, None
    hits: list[int] = []
    for index in range(start, end):
        line = lines[index]
        if not line.structural or line.indent != level:
            continue
        if line.body.rstrip() == "---":
            continue
        match = _ENTRY.fullmatch(line.body)
        if match is None:
            return False, None
        if match.group("key") == key:
            hits.append(index)
    if len(hits) > 1:
        return False, None
    return True, (hits[0] if hits else None)


def _insert(
    lines: list[_Line],
    start: int,
    end: int,
    parent_line: int | None,
    indent: int,
    path: list[str],
    rendered: str,
    newline: str,
) -> None:
    """Add *path* (one key or a chain of parents) at the end of its block."""
    step = _step(lines)
    last = _last_content(lines, start, end)
    if last is None:
        # An empty block: straight under its key, or at the end of an empty file.
        at = parent_line + 1 if parent_line is not None else len(lines)
    else:
        at = last + 1
    new: list[_Line] = []
    for depth, key in enumerate(path):
        pad = " " * (indent + depth * step)
        if depth == len(path) - 1:
            new.append(_Line(f"{pad}{key}: {rendered}", newline))
        else:
            new.append(_Line(f"{pad}{key}:", newline))
    if at > 0 and not lines[at - 1].end:
        lines[at - 1].end = newline
    lines[at:at] = new


def _replace_value(line: _Line, match: re.Match[str], value: Scalar, rendered: str) -> bool:
    """Put the new value where the old one was, keeping quotes and comment."""
    rest = match.group("rest")
    parts = _split_value(rest)
    if parts is None:
        return False
    old, comment = parts
    head = line.body[: match.start("rest")]
    lead = (rest[: len(rest) - len(rest.lstrip(" "))] if old else "") or " "
    if isinstance(value, str) and "\n" not in value and old[:1] == '"':
        rendered = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, str) and "\n" not in value and old[:1] == "'":
        rendered = "'" + value.replace("'", "''") + "'"
    if comment:
        # Keep the gap before the comment as it was, so aligned comments stay aligned.
        after_value = rest.lstrip(" ")[len(old):]
        gap = after_value[: len(after_value) - len(after_value.lstrip(" "))] or " "
        line.body = f"{head}{lead}{rendered}{gap}{comment}"
    else:
        line.body = f"{head}{lead}{rendered}"
    return True


def _split_value(rest: str) -> tuple[str, str] | None:
    """``(value, comment)`` from what follows a key's colon, or ``None`` if unsure.

    The value is returned exactly as written (quotes included); the comment
    starts with ``#``. Anything but a one-line plain or quoted scalar is
    refused.
    """
    text = rest.lstrip(" ")
    if not text or text.startswith("#"):
        return "", text
    opener = text[0]
    if opener in _UNSUPPORTED_OPENERS or text.startswith(("- ", "? ", ": ")):
        return None
    if opener == '"':
        close = _closing_double_quote(text)
    elif opener == "'":
        close = _closing_single_quote(text)
    else:
        found = re.search(r"\s#", text)
        value = text[: found.start()] if found else text
        comment = text[found.start():].lstrip() if found else ""
        return value.rstrip(), comment
    if close is None:
        return None  # a quoted value that carries on onto the next line
    value, remainder = text[: close + 1], text[close + 1:]
    stripped = remainder.lstrip(" ")
    if stripped and not stripped.startswith("#"):
        return None
    return value, stripped


def _closing_double_quote(text: str) -> int | None:
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index
        index += 1
    return None


def _closing_single_quote(text: str) -> int | None:
    index = 1
    while index < len(text):
        if text[index] == "'":
            if text[index + 1 : index + 2] == "'":
                index += 2
                continue
            return index
        index += 1
    return None


def _render(value: Scalar) -> str | None:
    """*value* as one line of YAML, quoted exactly where YAML needs it."""
    import yaml

    if not isinstance(value, (str, int, float, bool)) and value is not None:
        return None
    dumped = yaml.safe_dump(
        {"k": value}, default_flow_style=False, allow_unicode=True, width=1 << 30
    )
    text = dumped[len("k: "):].rstrip("\n")
    return None if "\n" in text else text
