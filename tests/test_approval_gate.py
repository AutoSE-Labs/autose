"""DR-012: approval gating is shared, fail-closed, and auditable."""

from __future__ import annotations

import pytest

from logic.common.approval import (
    APPROVAL_REQUIRED_TOOLS,
    ApprovalDecision,
    ApprovalRequest,
    AutoApproveGate,
    CallbackApprovalGate,
    DenyingApprovalGate,
    requires_approval,
)
from logic.common.events import RecordingEventSink
from logic.common.execution import ToolExecutor
from logic.common.tool import Tool, clone_tools
from logic.common.tools import catalog_subset
from tests.support.fakes import RecordingApprovalGate, tool_call


def _executor(workspace, *, gate=None, events=None, tools=None) -> ToolExecutor:
    registry = clone_tools(tools or catalog_subset("run_command", "read_file"))
    return ToolExecutor(
        registry, workspace=workspace, approval_gate=gate, events=events
    )


def test_the_approval_required_set_is_the_documented_one():
    assert frozenset(
        {"run_command", "web_search", "fetch_url"}
    ) == APPROVAL_REQUIRED_TOOLS
    assert requires_approval("run_command")
    assert not requires_approval("read_file")


def test_a_missing_gate_denies_rather_than_executing(workspace, tmp_path):
    marker = workspace.root / "ran.txt"
    executor = _executor(workspace)
    result = executor.execute(tool_call("run_command", command="touch ran.txt"))
    assert not result.approved
    assert not result.executed
    assert not marker.exists()
    assert "Denied" in result.content


def test_a_denied_call_never_reaches_the_handler(workspace):
    executed: list[str] = []
    spy = Tool(
        name="run_command",
        description="d",
        parameters={"command": {"type": "string", "description": "c"}},
        required=["command"],
        fn=lambda command, **kwargs: executed.append(command) or "ran",
    )
    executor = _executor(
        workspace, gate=RecordingApprovalGate(False), tools={"run_command": spy}
    )
    executor.execute(tool_call("run_command", command="rm -rf ."))
    assert executed == []


def test_a_denial_tells_the_model_not_to_retry_unchanged(workspace):
    executor = _executor(workspace, gate=RecordingApprovalGate(False))
    content = executor.execute(tool_call("run_command", command="ls")).content
    assert "was not executed" in content
    assert "Do not retry it unchanged" in content


def test_an_approved_call_executes(workspace):
    executor = _executor(workspace, gate=RecordingApprovalGate(True))
    result = executor.execute(tool_call("run_command", command="echo hi"))
    assert result.approved and result.executed
    assert "hi" in result.content


def test_the_user_sees_the_exact_command_not_a_generic_notice(workspace):
    gate = RecordingApprovalGate(True)
    _executor(workspace, gate=gate).execute(
        tool_call("run_command", command="git status --short")
    )
    assert gate.requests[0].summary == "git status --short"


def test_url_and_query_arguments_are_surfaced_verbatim():
    assert ApprovalRequest("fetch_url", {"url": "https://x.test/a"}).summary == (
        "https://x.test/a"
    )
    assert ApprovalRequest("web_search", {"query": "how to x"}).summary == "how to x"


def test_a_call_with_no_recognised_argument_still_summarises_something():
    summary = ApprovalRequest("run_command", {"weird": 1}).summary
    assert "weird" in summary


def test_both_halves_of_the_exchange_are_emitted_as_events(workspace):
    events = RecordingEventSink()
    _executor(workspace, gate=RecordingApprovalGate(True), events=events).execute(
        tool_call("run_command", command="echo audit")
    )
    requested = events.of_type("approval_requested")
    resolved = events.of_type("approval_resolved")
    assert len(requested) == len(resolved) == 1
    assert requested[0].data["summary"] == "echo audit"
    assert resolved[0].data["approved"] is True


def test_a_denial_is_audited_too(workspace):
    events = RecordingEventSink()
    _executor(workspace, gate=RecordingApprovalGate(False), events=events).execute(
        tool_call("run_command", command="rm -rf /")
    )
    assert events.of_type("approval_resolved")[0].data["approved"] is False
    assert not events.of_type("tool_called")


def test_a_non_approval_tool_is_not_gated(workspace):
    gate = RecordingApprovalGate(False)
    (workspace.root / "m.txt").write_text("body", encoding="utf-8")
    result = _executor(workspace, gate=gate).execute(tool_call("read_file", path="m.txt"))
    assert gate.requests == []
    assert "body" in result.content


def test_auto_approve_requires_naming_who_opted_in():
    with pytest.raises(ValueError):
        AutoApproveGate(authorized_by="")
    gate = AutoApproveGate(authorized_by="ci-pipeline")
    decision = gate.request(ApprovalRequest("run_command", {"command": "make"}))
    assert decision.approved
    assert "ci-pipeline" in decision.reason


def test_the_default_gate_denies_everything():
    decision = DenyingApprovalGate().request(ApprovalRequest("run_command", {}))
    assert not decision.approved


def test_a_callback_gate_accepts_a_bare_boolean():
    gate = CallbackApprovalGate(lambda request: True)
    assert gate.request(ApprovalRequest("run_command", {})).approved


def test_a_crashing_approval_interface_denies_rather_than_propagating():
    def broken(request):
        raise RuntimeError("window closed")

    decision = CallbackApprovalGate(broken).request(ApprovalRequest("run_command", {}))
    assert not decision.approved
    assert "window closed" in decision.reason


def test_a_crashing_gate_inside_the_executor_denies(workspace):
    class Exploding:
        def request(self, request):
            raise RuntimeError("boom")

    result = _executor(workspace, gate=Exploding()).execute(
        tool_call("run_command", command="echo x")
    )
    assert not result.approved
    assert "boom" in result.content


def test_an_approval_decision_can_carry_a_reason():
    assert ApprovalDecision.deny("policy").reason == "policy"
    assert ApprovalDecision.allow().approved
