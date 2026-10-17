"""The single enforcement point every tool call passes through.

Three cross-cutting rules meet here rather than being re-implemented per stage
or per Adapter:

* **DR-008** — a call naming a tool absent from *this stage's* registry is
  refused. A validation stage does not merely lack a description telling it
  about ``edit_file``; the call fails.
* **DR-012** — a call to an approval-required tool is surfaced with its actual
  arguments, blocks on a decision, and on denial returns an explanatory string
  *without ever reaching the handler*. Both halves of the exchange are emitted
  as audit events.
* **DR-011** — an oversized result is preserved in full and previewed
  head-and-tail.

Because the executor owns all three, a new Adapter inherits them by
construction: there is no path to a tool result that goes around it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..inference.types import ToolCall
from .approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalPolicy,
    ApprovalRequest,
    DenyingApprovalGate,
    denial_result,
)
from .events import Event, EventSink, NullEventSink
from .tool import Tool
from .tool_output import DEFAULT_MAX_TOOL_OUTPUT_CHARS, BoundedToolOutput
from .workspace import Workspace

__all__ = ["ToolExecutor", "ToolResult"]

#: Keyword arguments the executor injects itself. A model-supplied value for
#: one of these is dropped before dispatch, so a hallucinated (or deliberate)
#: ``workspace`` argument can never redirect a tool outside its sandbox.
_RESERVED_ARGUMENT_NAMES = frozenset({"workspace"})


class ToolResult:
    """What one tool call produced, plus how it was handled."""

    __slots__ = ("tool_name", "content", "approved", "executed", "artifact")

    def __init__(
        self,
        tool_name: str,
        content: str,
        *,
        approved: bool = True,
        executed: bool = True,
        artifact: str | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.content = content
        self.approved = approved
        self.executed = executed
        self.artifact = artifact


class ToolExecutor:
    """Dispatches tool calls against one agent's own cloned registry."""

    def __init__(
        self,
        tools: Mapping[str, Tool],
        *,
        workspace: Workspace,
        approval_gate: ApprovalGate | None = None,
        approval_policy: ApprovalPolicy | None = None,
        events: EventSink | None = None,
        bounded_output: BoundedToolOutput | None = None,
        max_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS,
    ) -> None:
        self._tools = dict(tools)
        self._workspace = workspace
        # Fail closed: an Adapter that supplies no gate gets denials, not
        # unreviewed shell execution (DR-012).
        self._approval_gate = approval_gate or DenyingApprovalGate()
        # DR-024: which tools are gated is a per-run value, not a global, so a
        # workspace requiring approval for edits cannot leak that stricter
        # posture into another session running in the same process.
        self._approval_policy = approval_policy or ApprovalPolicy.default()
        self._events = events or NullEventSink()
        self._output = bounded_output or BoundedToolOutput(max_chars=max_output_chars)

    # -- introspection --------------------------------------------------

    @property
    def tools(self) -> Mapping[str, Tool]:
        return self._tools

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    def has(self, tool_name: str) -> bool:
        return tool_name in self._tools

    @property
    def approval_policy(self) -> ApprovalPolicy:
        return self._approval_policy

    # -- execution -------------------------------------------------------

    def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call, returning the string the model should see."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                call.name,
                self._unknown_tool_message(call.name),
                executed=False,
            )
        if call.parse_error:
            return ToolResult(
                call.name,
                f"Error: {call.parse_error}. Re-issue the call with valid JSON arguments.",
                executed=False,
            )

        arguments = {
            key: value
            for key, value in call.arguments.items()
            if key not in _RESERVED_ARGUMENT_NAMES
        }
        if self._approval_policy.requires(call.name):
            request = ApprovalRequest(tool_name=call.name, arguments=arguments)
            decision = self._resolve_approval(request)
            if not decision.approved:
                return ToolResult(
                    call.name,
                    denial_result(request, decision),
                    approved=False,
                    executed=False,
                )

        self._emit("tool_called", f"{call.name}", tool=call.name, arguments=arguments)
        raw = self._invoke(tool, arguments)
        content, artifact = self._output.apply(call.name, arguments, raw)
        self._emit(
            "tool_completed",
            call.name,
            tool=call.name,
            result_chars=len(raw),
            truncated=artifact is not None or len(content) < len(raw),
            artifact=artifact,
        )
        return ToolResult(call.name, content, artifact=artifact)

    # -- internals --------------------------------------------------------

    def _resolve_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Ask, record both halves, and return the decision (DR-012 step 4)."""
        self._emit(
            "approval_requested",
            f"{request.tool_name}: {request.summary}",
            **request.to_dict(),
        )
        try:
            decision = self._approval_gate.request(request)
        except Exception as exc:  # noqa: BLE001 - a broken gate must deny, not crash
            decision = ApprovalDecision.deny(f"approval gate failed: {exc}")
        self._emit(
            "approval_resolved",
            f"{request.tool_name}: {'approved' if decision.approved else 'denied'}",
            tool=request.tool_name,
            summary=request.summary,
            approved=decision.approved,
            reason=decision.reason,
        )
        return decision

    def _invoke(self, tool: Tool, arguments: dict[str, Any]) -> str:
        try:
            return tool(workspace=self._workspace, **arguments)
        except TypeError as exc:
            # Wrong or missing arguments: tell the model, do not fail the stage.
            return f"Error: invalid arguments for tool '{tool.name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - a tool fault is model-visible data
            return f"Error: tool '{tool.name}' failed: {exc}"

    def _unknown_tool_message(self, name: str) -> str:
        available = ", ".join(sorted(self._tools)) or "none"
        return (
            f"Error: '{name}' is not available at this stage. "
            f"Available tools: {available}."
        )

    def _emit(self, event_type: str, message: str = "", **data: Any) -> None:
        self._events.emit(Event(type=event_type, message=message, data=data))
