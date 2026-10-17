"""The shared enforcement point: dispatch, permissions, and error containment."""

from __future__ import annotations

from logic.common.events import RecordingEventSink
from logic.common.execution import ToolExecutor
from logic.common.tool import Tool, clone_tools
from logic.common.tool_output import BoundedToolOutput, NullToolOutputStore
from logic.common.tools import catalog_subset
from logic.inference.types import ToolCall
from tests.support.fakes import RecordingApprovalGate, tool_call


def _executor(workspace, names=("read_file",), **kwargs) -> ToolExecutor:
    return ToolExecutor(
        clone_tools(catalog_subset(*names)), workspace=workspace, **kwargs
    )


def test_a_tool_absent_from_this_stage_is_refused_by_name(workspace):
    result = _executor(workspace).execute(tool_call("edit_file", path="m.py", edits=[]))
    assert not result.executed
    assert "not available at this stage" in result.content
    assert "read_file" in result.content


def test_a_validation_style_registry_cannot_reach_a_write_tool(workspace):
    executor = _executor(workspace, names=("read_file", "search_files"))
    target = workspace.root / "m.py"
    result = executor.execute(tool_call("write_file", path="m.py", content="x"))
    assert not result.executed
    assert not target.exists()


def test_unparsable_arguments_are_reported_to_the_model(workspace):
    call = ToolCall.from_payload(
        {"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}}
    )
    result = _executor(workspace).execute(call)
    assert not result.executed
    assert "could not parse tool arguments" in result.content


def test_arguments_sent_as_an_object_instead_of_a_string_are_accepted(workspace):
    (workspace.root / "m.txt").write_text("body", encoding="utf-8")
    call = ToolCall.from_payload(
        {"id": "c1", "function": {"name": "read_file", "arguments": {"path": "m.txt"}}}
    )
    assert "body" in _executor(workspace).execute(call).content


def test_wrong_arguments_return_an_error_rather_than_raising(workspace):
    result = _executor(workspace).execute(tool_call("read_file", nonexistent="x"))
    assert "invalid arguments" in result.content


def test_a_raising_handler_is_contained_as_a_tool_result(workspace):
    def explode(**kwargs):
        raise RuntimeError("handler bug")

    exploding = Tool(
        name="read_file", description="d", parameters={}, required=[], fn=explode
    )
    executor = ToolExecutor({"read_file": exploding}, workspace=workspace)
    result = executor.execute(tool_call("read_file"))
    assert "handler bug" in result.content


def test_the_workspace_is_injected_not_taken_from_the_model(workspace):
    seen: dict = {}

    def capture(*, workspace, **kwargs):
        seen["root"] = workspace.root
        return "ok"

    executor = ToolExecutor(
        {"probe": Tool("probe", "d", {}, [], capture)}, workspace=workspace
    )
    executor.execute(tool_call("probe", workspace="/etc"))
    assert seen["root"] == workspace.root


def test_call_and_completion_are_emitted_as_events(workspace):
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    events = RecordingEventSink()
    _executor(workspace, events=events).execute(tool_call("read_file", path="m.txt"))
    assert events.of_type("tool_called")[0].data["tool"] == "read_file"
    assert events.of_type("tool_completed")[0].data["truncated"] is False


def test_oversized_results_are_bounded_and_flagged(workspace):
    big = Tool("probe", "d", {}, [], lambda **kwargs: "y" * 20_000)
    events = RecordingEventSink()
    executor = ToolExecutor(
        {"probe": big},
        workspace=workspace,
        events=events,
        bounded_output=BoundedToolOutput(NullToolOutputStore(), max_chars=500),
    )
    result = executor.execute(tool_call("probe"))
    assert len(result.content) < 20_000
    assert events.of_type("tool_completed")[0].data["truncated"] is True


def test_approval_is_requested_before_the_tool_is_dispatched(workspace):
    events = RecordingEventSink()
    _executor(
        workspace,
        names=("run_command",),
        approval_gate=RecordingApprovalGate(True),
        events=events,
    ).execute(tool_call("run_command", command="echo x"))
    types = [event.type for event in events.events]
    assert types.index("approval_requested") < types.index("tool_called")
