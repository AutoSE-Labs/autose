"""The ``Tool`` definition and per-agent registry cloning (DR-008).

Module-level tool definitions elsewhere in this package are *immutable
catalog entries*. An agent never holds a reference to the catalog: its
constructor calls :func:`clone_tools` to take its own copy, optionally
swapping in instrumented handlers. Because the clone is discarded with the
agent, a runner that wraps a handler to capture diffs needs no restoration
step, and two concurrent runs cannot corrupt each other's tool state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = ["Tool", "ToolHandler", "clone_tools", "tools_schema"]

#: A tool handler receives the model-supplied arguments as keyword arguments,
#: plus a ``workspace`` keyword injected by the executor, and returns the text
#: the model will see as the tool result.
ToolHandler = Callable[..., str]


@dataclass
class Tool:
    """One model-callable function: its schema and its Python implementation."""

    name: str
    description: str
    #: JSON Schema ``properties`` mapping: parameter name -> schema object.
    parameters: dict[str, dict]
    #: Names of the required parameters.
    required: list[str]
    fn: ToolHandler = field(repr=False)

    def __call__(self, **kwargs: Any) -> str:
        return self.fn(**kwargs)

    def clone(self, fn: ToolHandler | None = None) -> Tool:
        """Return an independent copy, optionally with a replacement handler.

        The schema containers are copied so an agent cannot mutate the catalog
        entry it was cloned from; the handler itself is shared by reference
        unless *fn* overrides it (DR-008 explicitly rejects deep-copying
        everything reachable from a tool).
        """
        return replace(
            self,
            parameters=deepcopy(self.parameters),
            required=list(self.required),
            fn=fn if fn is not None else self.fn,
        )

    def to_openai_schema(self) -> dict:
        """Return this tool in the OpenAI-compatible function-calling shape (DR-006)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": deepcopy(self.parameters),
                    "required": list(self.required),
                },
            },
        }


def clone_tools(
    tools: Mapping[str, Tool],
    overrides: Mapping[str, ToolHandler] | None = None,
) -> dict[str, Tool]:
    """Return an agent-owned registry cloned from immutable catalog definitions.

    *overrides* maps a tool name to a replacement handler; a name that is not
    present in *tools* is ignored, so instrumenting an optional tool never
    smuggles it into a stage whose registry deliberately excludes it.
    """
    handlers = overrides or {}
    return {
        name: tool.clone(fn=handlers.get(name)) for name, tool in tools.items()
    }


def tools_schema(tools: Iterable[Tool] | Mapping[str, Tool]) -> list[dict]:
    """Render a registry into the ``tools`` array of a chat-completions request."""
    values = tools.values() if isinstance(tools, Mapping) else tools
    return [tool.to_openai_schema() for tool in values]
