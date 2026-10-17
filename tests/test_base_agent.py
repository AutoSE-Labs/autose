"""The shared agent loop: rounds, budgeting, permissions, streaming."""

from __future__ import annotations

import pytest

from logic.common.agent import BaseAgent
from logic.common.context import ContextBudget
from logic.common.events import RecordingEventSink
from logic.common.tools import catalog_subset
from logic.errors import ContextBudgetError
from logic.inference.types import CompletionResult, Usage
from tests.support.fakes import (
    RecordingApprovalGate,
    ScriptedChatModel,
    answer,
    calls,
    tool_call,
)


class _Stage(BaseAgent):
    name = "test-stage"

    def system_prompt(self) -> str:
        return "You are a test stage."


def _agent(workspace, script, *, tools=None, **kwargs) -> _Stage:
    return _Stage(
        model=ScriptedChatModel(script),
        tools=tools if tools is not None else catalog_subset("read_file"),
        workspace=workspace,
        **kwargs,
    )


def test_a_direct_answer_needs_one_round(workspace):
    agent = _agent(workspace, [answer("done")])
    result = agent.run("hello")
    assert result.content == "done"
    assert result.stop_reason == "answered"
    assert result.complete
    assert result.rounds == 1
    assert result.tool_calls == 0


def test_the_loop_executes_tools_then_answers(workspace):
    (workspace.root / "m.txt").write_text("file body", encoding="utf-8")
    agent = _agent(
        workspace,
        [calls(tool_call("read_file", path="m.txt")), answer("summarised")],
    )
    result = agent.run("read it")
    assert result.content == "summarised"
    assert result.tool_calls == 1
    assert result.rounds == 2


def test_the_tool_result_is_fed_back_as_a_tool_message(workspace):
    (workspace.root / "m.txt").write_text("file body", encoding="utf-8")
    model = ScriptedChatModel(
        [calls(tool_call("read_file", path="m.txt")), answer("ok")]
    )
    _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace).run("go")

    second_request = model.requests[1]["messages"]
    assert second_request[-1]["role"] == "tool"
    assert "file body" in second_request[-1]["content"]
    assert second_request[-2]["role"] == "assistant"


def test_the_system_prompt_leads_every_request(workspace):
    model = ScriptedChatModel([answer("ok")])
    _Stage(model=model, tools={}, workspace=workspace).run("go")
    messages = model.requests[0]["messages"]
    assert messages[0] == {"role": "system", "content": "You are a test stage."}
    assert messages[-1] == {"role": "user", "content": "go"}


def test_session_context_is_injected_between_system_and_user(workspace):
    model = ScriptedChatModel([answer("ok")])
    _Stage(model=model, tools={}, workspace=workspace).run(
        "now", session_context=[{"role": "user", "content": "earlier"}]
    )
    roles = [message["content"] for message in model.requests[0]["messages"]]
    assert roles == ["You are a test stage.", "earlier", "now"]


def test_the_round_cap_stops_a_tool_loop_and_says_so(workspace):
    # A different file each round, so it is the configured cap that stops this
    # and not the no-progress guard.
    for index in range(4):
        (workspace.root / f"m{index}.txt").write_text(f"x{index}", encoding="utf-8")
    script = [
        calls(tool_call("read_file", path=f"m{index}.txt"), content="still looking")
        for index in range(4)
    ]
    events = RecordingEventSink()
    agent = _agent(workspace, script, max_tool_rounds=4, events=events)
    result = agent.run("loop forever")
    assert result.stop_reason == "round_cap"
    assert not result.complete
    assert result.content == "still looking"
    assert events.of_type("agent_round_cap_reached")


def test_a_repeated_call_is_not_actually_executed_again(workspace):
    """A stalled small local model re-issues the same call; past the
    threshold it is refused instead of executed again, so the loop is told to
    move on rather than spending another round on evidence it already has."""
    calls_made = []

    def _counting_read(*, path, **kwargs):
        calls_made.append(path)
        return "still looking"

    script = [
        calls(tool_call("read_file", path="m.txt"), content="still looking")
        for _ in range(3)
    ] + [answer("done")]
    agent = _agent(
        workspace, script, tool_overrides={"read_file": _counting_read}
    )
    result = agent.run("loop")
    assert result.content == "done"
    # Two calls actually ran; the third and fourth were refused outright.
    assert len(calls_made) == 2


def test_a_call_with_different_arguments_is_not_treated_as_a_repeat(workspace):
    (workspace.root / "a.txt").write_text("a", encoding="utf-8")
    (workspace.root / "b.txt").write_text("b", encoding="utf-8")
    script = [
        calls(tool_call("read_file", path="a.txt"), content="looking"),
        calls(tool_call("read_file", path="b.txt"), content="looking"),
        calls(tool_call("read_file", path="a.txt"), content="looking"),
        answer("done"),
    ]
    events = RecordingEventSink()
    result = _agent(workspace, script, events=events).run("go")
    assert result.content == "done"
    assert not events.of_type("tool_call_repeated")


def test_a_write_tool_clears_the_repeat_count_for_read_only_calls(workspace):
    (workspace.root / "m.txt").write_text("old", encoding="utf-8")
    tools = catalog_subset("read_file", "edit_file")
    script = [
        calls(tool_call("read_file", path="m.txt"), content="looking"),
        calls(
            tool_call(
                "edit_file", path="m.txt", edits=[{"old_str": "old", "new_str": "new"}]
            ),
            content="editing",
        ),
        calls(tool_call("read_file", path="m.txt"), content="looking again"),
        answer("done"),
    ]
    events = RecordingEventSink()
    result = _agent(workspace, script, tools=tools, events=events).run("go")
    assert result.content == "done"
    assert not events.of_type("tool_call_repeated")


#: The catalog's own handler, captured before any agent is built, so the test
#: below can prove the catalog entry survives an agent instrumenting its clone.
_CATALOG_READ_FILE = catalog_subset("read_file")["read_file"].fn


def test_each_agent_owns_its_cloned_registry(workspace):
    catalog = catalog_subset("read_file")
    instrumented: list[str] = []
    first = _agent(
        workspace,
        [answer("a")],
        tools=catalog,
        tool_overrides={
            "read_file": lambda path, **kwargs: instrumented.append(path) or "patched"
        },
    )
    second = _agent(workspace, [answer("b")], tools=catalog)

    # The catalog entry itself is never touched, and one agent's instrumented
    # handler is invisible to the other. An un-overridden handler is no longer
    # the *same object* as the catalog's — every agent binds its own
    # window-derived `max_chars` onto it — so identity against the catalog is
    # the wrong invariant; that the two agents differ, and that the catalog is
    # unchanged, is the one DR-008 actually asks for.
    assert catalog["read_file"].fn is _CATALOG_READ_FILE
    assert first.executor.tools["read_file"].fn is not catalog["read_file"].fn
    assert (
        first.executor.tools["read_file"].fn
        is not second.executor.tools["read_file"].fn
    )
    assert second.executor.tools["read_file"].fn.func is _CATALOG_READ_FILE


def test_a_stage_only_advertises_the_tools_it_holds(workspace):
    model = ScriptedChatModel([answer("ok")])
    _Stage(
        model=model,
        tools=catalog_subset("read_file", "search_files"),
        workspace=workspace,
    ).run("go")
    names = {
        entry["function"]["name"] for entry in model.requests[0]["tools"]
    }
    assert names == {"read_file", "search_files"}


def test_a_stage_with_no_tools_sends_no_tool_schema(workspace):
    model = ScriptedChatModel([answer("ok")])
    _Stage(model=model, tools={}, workspace=workspace).run("go")
    assert not model.requests[0]["tools"]


def test_a_context_budget_failure_propagates_as_a_typed_error(workspace):
    agent = _agent(
        workspace,
        [answer("never reached")],
        budget=ContextBudget(context_limit=10, reserved_output_tokens=10),
    )
    with pytest.raises(ContextBudgetError):
        agent.run("hello")


def test_reserved_output_capacity_is_sent_as_max_tokens(workspace):
    model = ScriptedChatModel([answer("ok")])
    _Stage(
        model=model,
        tools={},
        workspace=workspace,
        budget=ContextBudget(context_limit=100_000, reserved_output_tokens=512),
    ).run("go")
    assert model.requests[0]["max_output_tokens"] == 512


def test_usage_is_accumulated_across_rounds(workspace):
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    script = [
        CompletionResult(
            tool_calls=(tool_call("read_file", path="m.txt"),),
            usage=Usage(prompt_tokens=10, completion_tokens=2),
        ),
        answer("done", prompt_tokens=20, completion_tokens=5),
    ]
    result = _agent(workspace, script).run("go")
    assert result.usage.prompt_tokens == 30
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 37


def test_context_metrics_are_recorded_per_round(workspace):
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    agent = _agent(
        workspace,
        [calls(tool_call("read_file", path="m.txt")), answer("done")],
        budget=ContextBudget(context_limit=100_000),
    )
    result = agent.run("go")
    assert len(result.context_metrics) == 2
    assert all(metric.context_limit == 100_000 for metric in result.context_metrics)


def test_a_tool_call_written_as_prose_is_recovered(workspace):
    (workspace.root / "m.txt").write_text("recovered body", encoding="utf-8")
    agent = _agent(
        workspace,
        [answer("call:read_file{path:m.txt}"), answer("done")],
    )
    result = agent.run("go")
    assert result.tool_calls == 1
    assert result.content == "done"


def test_prose_naming_a_tool_this_stage_lacks_is_left_as_an_answer(workspace):
    agent = _agent(workspace, [answer("call:edit_file{path:m.py}")])
    result = agent.run("go")
    assert result.tool_calls == 0
    assert result.content == "call:edit_file{path:m.py}"


def test_approval_gating_applies_inside_the_agent_loop(workspace):
    gate = RecordingApprovalGate(False)
    agent = _agent(
        workspace,
        [calls(tool_call("run_command", command="rm -rf .")), answer("adapted")],
        tools=catalog_subset("run_command"),
        approval_gate=gate,
    )
    result = agent.run("go")
    assert gate.requests[0].summary == "rm -rf ."
    assert result.content == "adapted"


def test_an_agent_with_no_approval_gate_denies_side_effecting_calls(workspace):
    marker = workspace.root / "ran.txt"
    agent = _agent(
        workspace,
        [calls(tool_call("run_command", command="touch ran.txt")), answer("blocked")],
        tools=catalog_subset("run_command"),
    )
    agent.run("go")
    assert not marker.exists()


def test_streaming_yields_text_and_then_exposes_the_result(workspace):
    agent = _agent(workspace, [answer("streamed answer")])
    stream = agent.stream("go")
    text = "".join(stream)
    assert text == "streamed answer"
    assert stream.result.content == "streamed answer"
    assert stream.result.stop_reason == "answered"


def test_streaming_runs_tool_rounds_without_a_second_generation(workspace):
    (workspace.root / "m.txt").write_text("body", encoding="utf-8")
    model = ScriptedChatModel(
        [calls(tool_call("read_file", path="m.txt")), answer("final")]
    )
    agent = _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace)
    text = "".join(agent.stream("go"))
    assert text == "final"
    assert model.call_count == 2


def test_subclasses_must_define_a_system_prompt(workspace):
    class _Bare(BaseAgent):
        pass

    agent = _Bare(
        model=ScriptedChatModel([answer("x")]), tools={}, workspace=workspace
    )
    with pytest.raises(NotImplementedError):
        agent.run("go")


# -- DR-022: the runtime served less than it advertised ----------------------


def test_a_runtime_that_silently_drops_input_is_reported(workspace):
    """DR-022: a truncating runtime is invisible without this check.

    The request is not rejected — it is cut from the front, taking the system
    prompt with it — so the only evidence is the provider's own token count
    coming back far below what was sent.
    """
    events = RecordingEventSink()
    agent = _agent(
        workspace,
        [answer("done", prompt_tokens=40)],
        events=events,
        # A long prompt so the estimate is comfortably above the reported count.
        budget=ContextBudget(context_limit=100_000),
    )
    agent.run("x" * 20_000)

    reported = [
        event for event in events.events if event.type == "context_truncated_by_runtime"
    ]
    assert len(reported) == 1
    assert reported[0].data["reported_prompt_tokens"] == 40
    assert reported[0].data["estimated_input_tokens"] > 40


def test_ordinary_tokenizer_drift_does_not_warn(workspace):
    """The estimate runs high by design; a normal gap must stay silent."""
    events = RecordingEventSink()
    agent = _agent(
        workspace,
        [answer("done", prompt_tokens=5_000)],
        events=events,
        budget=ContextBudget(context_limit=100_000),
    )
    agent.run("x" * 20_000)

    assert not [
        event for event in events.events if event.type == "context_truncated_by_runtime"
    ]


# -- DR-033: usage reaches an adapter while the stage is still running -------


def test_every_round_reports_its_usage_as_it_happens(workspace):
    """DR-033: a dashboard that only hears about tokens when a stage ends shows
    zero for the whole of a long stage, which reads as "nothing is happening".
    """
    (workspace.root / "m.txt").write_text("file body", encoding="utf-8")
    events = RecordingEventSink()
    agent = _agent(
        workspace,
        [
            calls(tool_call("read_file", path="m.txt")),
            answer("done", prompt_tokens=120, completion_tokens=7),
        ],
        events=events,
        budget=ContextBudget(context_limit=32_768),
    )
    agent.run("read it")

    rounds = events.of_type("round_usage")
    assert [event.data["round"] for event in rounds] == [1, 2]
    assert all(event.data["stage"] == "test-stage" for event in rounds)
    assert rounds[1].data["prompt_tokens"] == 120
    assert rounds[1].data["completion_tokens"] == 7
    # The budget's estimate stands in for a runtime that reports no usage.
    assert rounds[0].data["estimated_input_tokens"] > 0
    assert rounds[0].data["context_limit"] == 32_768


# -- DR-037: a step's result reaches an adapter, bounded ---------------------


def test_a_completed_tool_call_carries_a_bounded_preview_of_its_result(workspace):
    """DR-037: the inspector shows what came back without re-reading anything."""
    (workspace.root / "m.txt").write_text("file body " * 400, encoding="utf-8")
    events = RecordingEventSink()
    agent = _agent(
        workspace,
        [calls(tool_call("read_file", path="m.txt")), answer("done")],
        events=events,
    )
    agent.run("read it")

    (called,) = events.of_type("tool_called")
    (completed,) = events.of_type("tool_completed")
    assert completed.data["call_id"] == called.data["call_id"]
    assert "file body" in completed.data["preview"]
    assert len(completed.data["preview"]) <= 1_200 + 2
    assert completed.data["failed"] is False
    assert completed.data["elapsed_ms"] >= 0


def test_a_failed_tool_call_is_marked_failed(workspace):
    events = RecordingEventSink()
    agent = _agent(
        workspace,
        [calls(tool_call("read_file", path="missing.txt")), answer("done")],
        events=events,
    )
    agent.run("read it")
    (completed,) = events.of_type("tool_completed")
    assert completed.data["failed"] is True


# -- DR-039: a tool call the runtime could not parse is retried --------------


def _malformed() -> Exception:
    from logic.errors import MalformedToolCallError

    return MalformedToolCallError(
        "XML syntax error on line 3: element <function> closed by </parameter>"
    )


def test_a_malformed_tool_call_is_retried_with_what_was_wrong(workspace):
    """DR-039: seen live — Ollama's parser rejected a Qwen tool call mid-stream
    and the whole run stopped at Plan. One bad generation is not a failure."""
    events = RecordingEventSink()
    model = ScriptedChatModel([_malformed(), answer("done")])
    agent = _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace, events=events)

    result = agent.run("read something")

    assert result.content == "done"
    (event,) = events.of_type("tool_call_malformed")
    assert "XML syntax error" in event.message
    retried = model.requests[-1]["messages"][-1]
    assert retried["role"] == "user"
    assert "could not parse" in retried["content"]


def test_a_model_that_keeps_writing_malformed_calls_fails_the_stage(workspace):
    from logic.errors import MalformedToolCallError

    model = ScriptedChatModel([_malformed() for _ in range(4)])
    agent = _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace)

    with pytest.raises(MalformedToolCallError):
        agent.run("read something")
    assert model.call_count == 4
