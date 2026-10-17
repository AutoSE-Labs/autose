"""Approval gating for side-effecting and network tools (DR-012).

``run_command`` executes an arbitrary shell command; ``web_search`` and
``fetch_url`` reach onto the network and pull back untrusted content. DR-012
requires that the decision "may this *specific* call run" be:

* defined **once**, here, and read by every Adapter — never re-declared per
  client, so a new interface inherits the gate instead of reimplementing it;
* enforced in code every client passes through (see
  :mod:`logic.common.execution`), not in prompt prose the model can be steered
  around;
* fail-closed by default, with auto-approval only ever a deliberate opt-in.

This module owns the *policy vocabulary*. The enforcement point — emitting the
audit events and refusing to reach the underlying handler on a deny — lives in
``ToolExecutor`` so no caller can obtain a tool result while skipping it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "APPROVAL_REQUIRED_TOOLS",
    "EDIT_APPROVAL_TOOLS",
    "ApprovalPolicy",
    "requires_approval",
    "ApprovalRequest",
    "ApprovalDecision",
    "ApprovalGate",
    "DenyingApprovalGate",
    "AutoApproveGate",
    "CallbackApprovalGate",
    "denial_result",
]

#: The single shared definition of which tools may not execute unreviewed.
#: Read by every Adapter; never re-declared or re-decided in one (DR-012).
APPROVAL_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {"run_command", "web_search", "fetch_url"}
)

#: The tools that rewrite the workspace. Not gated by default — DR-024 makes
#: them reviewable and reversible after the fact instead, because a local
#: model makes many small edits and a prompt per edit is a prompt people learn
#: to answer without reading. A workspace that wants the stricter posture opts
#: in, and then these join the set above.
EDIT_APPROVAL_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})

#: Argument names that carry the substance of an approval-required call, in the
#: order they should be preferred when summarising it for a human. ``path``
#: last: a call with a command or a URL is described by that, and only an edit
#: falls through to being described by the file it touches.
_SUMMARY_KEYS: tuple[str, ...] = ("command", "url", "query", "path")


@dataclass(frozen=True)
class ApprovalPolicy:
    """Which tools this run may not execute unreviewed.

    A value rather than a module-level constant because the answer is now a
    per-run choice: DR-024 lets a workspace require approval for file edits
    too. Keeping it a value means the executor is handed the policy that
    applies, rather than consulting a global that a second concurrent session
    could disagree with.
    """

    tools: frozenset[str] = APPROVAL_REQUIRED_TOOLS

    @classmethod
    def default(cls) -> ApprovalPolicy:
        """Shell and network gated; edits journalled and reversible (DR-024)."""
        return cls()

    @classmethod
    def including_edits(cls) -> ApprovalPolicy:
        """Also gate `write_file`/`edit_file`, for a workspace that wants it."""
        return cls(tools=APPROVAL_REQUIRED_TOOLS | EDIT_APPROVAL_TOOLS)

    @classmethod
    def resolve(cls, *, require_edit_approval: bool = False) -> ApprovalPolicy:
        return cls.including_edits() if require_edit_approval else cls.default()

    def requires(self, tool_name: str) -> bool:
        return tool_name in self.tools

    @property
    def gates_edits(self) -> bool:
        return bool(EDIT_APPROVAL_TOOLS & self.tools)


def requires_approval(tool_name: str) -> bool:
    """Whether *tool_name* is gated under the default policy.

    Retained as the module-level answer for callers describing AutoSE's
    posture in general (an Adapter listing which tools will interrupt). A
    specific run is governed by its :class:`ApprovalPolicy`, which may be
    stricter.
    """
    return tool_name in APPROVAL_REQUIRED_TOOLS


@dataclass(frozen=True)
class ApprovalRequest:
    """The concrete call being put to the user — not a generic notice.

    DR-012 requires the user see the *actual* arguments: the exact shell
    command, the exact URL or query. :attr:`summary` is that string.
    """

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """A one-line rendering of exactly what this call would do."""
        for key in _SUMMARY_KEYS:
            value = self.arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            return json.dumps(dict(self.arguments), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(dict(self.arguments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "arguments": dict(self.arguments),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    """The resolution of one :class:`ApprovalRequest`."""

    approved: bool
    #: Short, user-facing explanation. Surfaced to the model on a denial so it
    #: can adapt rather than silently retrying the same call.
    reason: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> ApprovalDecision:
        return cls(approved=True, reason=reason)

    @classmethod
    def deny(cls, reason: str = "") -> ApprovalDecision:
        return cls(approved=False, reason=reason)


@runtime_checkable
class ApprovalGate(Protocol):
    """Strategy seam: how an approval decision is obtained.

    Adding a new approval policy (a TUI prompt, a GUI modal, a CI allowlist)
    means writing one implementation of this Protocol and handing it to the
    executor — no edit to any code that requests approval.
    """

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        """Block until *request* is approved or denied, then return the decision."""


class DenyingApprovalGate:
    """Denies every approval-required call. The fail-closed default.

    An Adapter that forgets to supply a gate gets refusals, never silent
    execution — the failure mode DR-012 exists to make structurally impossible.
    """

    _REASON = (
        "no approval interface is attached to this run; "
        "approval-required tools are refused by default"
    )

    def request(self, request: ApprovalRequest) -> ApprovalDecision:  # Protocol implementation.
        return ApprovalDecision.deny(self._REASON)


class AutoApproveGate:
    """Approves every request without asking — unattended/CI use only.

    DR-012 permits this **only** as a deliberate, explicit opt-in for a
    specific run. Constructing it requires naming who turned it on, so an
    auto-approved call is attributable in the audit trail and a copy-pasted CI
    template cannot leave it on by accident and unlabelled.
    """

    def __init__(self, *, authorized_by: str) -> None:
        if not authorized_by or not authorized_by.strip():
            raise ValueError(
                "AutoApproveGate requires an explicit 'authorized_by' identifying "
                "who opted into unattended approval for this run (DR-012)."
            )
        self._authorized_by = authorized_by.strip()

    @property
    def authorized_by(self) -> str:
        return self._authorized_by

    def request(self, request: ApprovalRequest) -> ApprovalDecision:  # Protocol implementation.
        return ApprovalDecision.allow(
            f"auto-approved for this run by {self._authorized_by}"
        )


class CallbackApprovalGate:
    """Delegates the decision to a callable supplied by an Adapter.

    The callable blocks until a human answers. Any exception it raises — a
    closed window, an interrupted terminal — resolves to a denial, keeping the
    fail-closed posture even when the interface breaks mid-prompt.
    """

    def __init__(
        self, callback: Callable[[ApprovalRequest], ApprovalDecision | bool]
    ) -> None:
        self._callback = callback

    def request(self, request: ApprovalRequest) -> ApprovalDecision:  # Protocol implementation.
        try:
            outcome = self._callback(request)
        except Exception as exc:  # noqa: BLE001 - see class docstring
            return ApprovalDecision.deny(f"approval interface failed: {exc}")
        if isinstance(outcome, ApprovalDecision):
            return outcome
        return ApprovalDecision(approved=bool(outcome))


def denial_result(request: ApprovalRequest, decision: ApprovalDecision) -> str:
    """The tool result a denied call returns to the model.

    Explicit rather than empty, so the model can adapt (ask, or choose another
    approach) instead of retrying the identical call in a loop.
    """
    reason = decision.reason.strip()
    detail = f" Reason: {reason}." if reason else ""
    return (
        f"Denied: the user did not approve this '{request.tool_name}' call "
        f"({request.summary}).{detail} The call was not executed. Do not retry it "
        "unchanged — proceed another way, or explain what you need approved and why."
    )
