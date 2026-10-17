"""Fallback parsing for models that emit tool calls as prose.

DR-006 commits AutoSE to running against small and self-hosted models, whose
tool-calling reliability is well below a frontier hosted model's. A common
failure is emitting the call as text in the ``content`` field instead of
populating ``tool_calls``. Recovering that one shape costs a regex and turns a
wasted round into a working one.

The recognised form is::

    call:tool_name{key:value, key:value}

Only a call naming a tool the stage actually holds is accepted, so this path
can never conjure a tool the registry deliberately withheld (DR-008).
"""

from __future__ import annotations

import json
import re
from collections.abc import Container

from ..inference.types import ToolCall

__all__ = ["parse_text_tool_call"]

_TEXT_CALL = re.compile(r"call:(\w+)\{([^}]*)\}")
#: Split on a comma only when what follows looks like a new ``key:`` pair, so a
#: value containing commas (a shell command, a regex) survives intact.
_ARGUMENT_SPLIT = re.compile(r",\s*(?=\w[\w\s]*:)")


def parse_text_tool_call(content: str, known_tools: Container[str]) -> ToolCall | None:
    """Return a tool call recovered from *content*, or ``None`` if there is none."""
    if not content:
        return None
    match = _TEXT_CALL.search(content)
    if not match:
        return None
    name = match.group(1)
    if name not in known_tools:
        return None

    arguments: dict[str, str] = {}
    for part in _ARGUMENT_SPLIT.split(match.group(2).strip()):
        part = part.strip()
        separator = part.find(":")
        if separator <= 0:
            continue
        key = part[:separator].strip()
        if key:
            arguments[key] = part[separator + 1 :].strip()
    if not arguments:
        return None

    return ToolCall(
        id="text_fallback_0",
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )
