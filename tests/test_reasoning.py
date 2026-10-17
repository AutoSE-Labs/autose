"""DR-016: chain of thought is a separate, non-replayed channel."""

from __future__ import annotations

import io
import json

import pytest

from logic.common.agent import BaseAgent
from logic.common.events import RecordingEventSink
from logic.common.tools import catalog_subset
from logic.inference.openai_compatible import (
    OpenAICompatibleChatModel,
    _ControlTokenFilter,
)
from logic.inference.settings import InferenceSettings
from logic.inference.types import CompletionResult, StreamChunk, ToolCall
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _sse(lines):
    return _FakeResponse("".join(f"{line}\n" for line in lines).encode("utf-8"))


class _Stage(BaseAgent):
    name = "test"

    def system_prompt(self) -> str:
        return "SYS"


# -- normalisation at the boundary --------------------------------------------


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "thinking"])
def test_every_known_reasoning_field_is_read(monkeypatch, field):
    payload = {
        "choices": [
            {"message": {"content": "answer", field: "the thought"}, "finish_reason": "stop"}
        ]
    }
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(json.dumps(payload).encode()),
    )
    result = OpenAICompatibleChatModel(
        InferenceSettings(base_url="http://x/v1", model="m")
    ).complete([])
    assert result.reasoning == "the thought"
    assert result.content == "answer"


def test_a_thinking_turn_with_tool_calls_keeps_all_three_parts(monkeypatch):
    """The real Ollama shape: empty content, reasoning, and a tool call."""
    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning": "I should read the file.",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "read_file", "arguments": '{"path":"m.py"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(json.dumps(payload).encode()),
    )
    result = OpenAICompatibleChatModel(
        InferenceSettings(base_url="http://x/v1", model="m")
    ).complete([])
    assert result.reasoning
    assert result.has_tool_calls
    assert not result.is_empty


def test_streamed_reasoning_arrives_on_its_own_channel(monkeypatch):
    """DR-014: one streamed round carries content, reasoning, and its result."""
    lines = [
        'data: {"choices":[{"delta":{"content":"","reasoning":"think"}}]}',
        'data: {"choices":[{"delta":{"content":"ans"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=None: _sse(lines))
    chunks = list(
        OpenAICompatibleChatModel(
            InferenceSettings(base_url="http://x/v1", model="m")
        ).stream([])
    )
    assert [c.reasoning for c in chunks if c.reasoning] == ["think"]
    assert [c.content for c in chunks if c.content] == ["ans"]
    assert chunks[-1].result.reasoning == "think"
    assert chunks[-1].result.content == "ans"


def test_usage_is_requested_on_streaming_calls(monkeypatch):
    captured: dict = {}

    def fake(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _sse(['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"])

    monkeypatch.setattr("urllib.request.urlopen", fake)
    list(
        OpenAICompatibleChatModel(
            InferenceSettings(base_url="http://x/v1", model="m")
        ).stream([])
    )
    assert captured["body"]["stream_options"] == {"include_usage": True}


# -- never replayed -------------------------------------------------------------


def test_reasoning_is_omitted_from_the_message_appended_to_history():
    message = CompletionResult(
        content="answer", reasoning="a very long deliberation"
    ).to_message()
    assert "reasoning" not in message
    assert message["content"] == "answer"


def test_a_second_round_does_not_carry_the_first_rounds_reasoning(workspace):
    (workspace.root / "m.txt").write_text("body", encoding="utf-8")
    model = ScriptedChatModel(
        [
            CompletionResult(
                tool_calls=(tool_call("read_file", path="m.txt"),),
                reasoning="I should read m.txt because the user asked about it.",
            ),
            answer("done"),
        ]
    )
    _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace).run("go")
    replayed = json.dumps(model.requests[1]["messages"])
    assert "I should read m.txt" not in replayed


# -- delivered as events, not as answer text -------------------------------------


def test_reasoning_reaches_adapters_as_events_not_as_the_answer(workspace):
    events = RecordingEventSink()
    model = ScriptedChatModel(
        [CompletionResult(content="the answer", reasoning="the thought", finish_reason="stop")]
    )
    agent = _Stage(model=model, tools={}, workspace=workspace, events=events)
    result = agent.run("go")
    assert result.content == "the answer"
    assert "the thought" not in result.content
    assert events.of_type("agent_reasoning")[0].message == "the thought"


def test_a_streamed_turn_yields_the_answer_only(workspace):
    events = RecordingEventSink()

    class _Reasoning:
        model = "m"

        def complete(self, messages, *, tools=None, max_output_tokens=None):
            raise AssertionError("should stream")

        def stream(self, messages, *, tools=None, max_output_tokens=None):
            yield StreamChunk(reasoning="think ")
            yield StreamChunk(reasoning="harder")
            yield StreamChunk(content="answer")
            yield StreamChunk(
                result=CompletionResult(
                    content="answer", reasoning="think harder", finish_reason="stop"
                )
            )

    agent = _Stage(model=_Reasoning(), tools={}, workspace=workspace, events=events)
    stream = agent.stream("go")
    assert "".join(stream) == "answer"
    deltas = [event.message for event in events.of_type("agent_reasoning_delta")]
    assert deltas == ["think ", "harder"]
    # The assembled block is not re-emitted on top of the per-delta events.
    assert not events.of_type("agent_reasoning")


# -- an empty turn is reported ----------------------------------------------------


def test_a_turn_that_only_thought_is_not_reported_as_a_successful_answer(workspace):
    """DR-016: a silent turn is reported, never rendered as a blank success."""
    silent = CompletionResult(content="", reasoning="thinking…", finish_reason="stop")
    model = ScriptedChatModel([silent, silent, silent])
    result = _Stage(model=model, tools={}, workspace=workspace).run("go")
    assert result.stop_reason == "empty_answer"
    assert not result.complete


def test_a_silent_turn_is_nudged_before_it_is_given_up_on(workspace):
    """DR-019: ask for the answer rather than discarding the gathered context."""
    events = RecordingEventSink()
    model = ScriptedChatModel(
        [
            CompletionResult(content="", reasoning="thinking…", finish_reason="stop"),
            answer("here is the answer"),
        ]
    )
    result = _Stage(model=model, tools={}, workspace=workspace, events=events).run("go")
    assert result.content == "here is the answer"
    assert result.complete
    assert len(events.of_type("agent_nudged")) == 1
    # The nudge is a turn in the same conversation, so the tool results the
    # model already collected are still in front of it.
    assert model.requests[1]["messages"][-1]["role"] == "user"


def test_a_silence_before_any_tool_call_is_told_to_start_not_to_stop(workspace):
    """Telling a stage that gathered nothing to stop using tools is worse than
    saying nothing (DR-019)."""
    events = RecordingEventSink()
    silent = CompletionResult(content="", reasoning="…", finish_reason="stop")
    model = ScriptedChatModel([silent, answer("done")])
    _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace, events=events).run("go")

    nudge = model.requests[1]["messages"][-1]["content"]
    assert "use your tools" in nudge
    assert "do not call any more tools" not in nudge
    assert events.of_type("agent_nudged")[0].data["gathered_evidence"] is False


def test_a_silence_after_gathering_is_told_to_stop_and_answer(workspace):
    (workspace.root / "m.txt").write_text("body", encoding="utf-8")
    events = RecordingEventSink()
    model = ScriptedChatModel(
        [
            calls(tool_call("read_file", path="m.txt")),
            CompletionResult(content="", reasoning="…", finish_reason="stop"),
            answer("done"),
        ]
    )
    _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace, events=events).run("go")

    nudge = model.requests[2]["messages"][-1]["content"]
    assert "do not call any more tools" in nudge
    assert events.of_type("agent_nudged")[0].data["gathered_evidence"] is True


def test_nudging_is_bounded(workspace):
    silent = CompletionResult(content="", reasoning="…", finish_reason="stop")
    events = RecordingEventSink()
    model = ScriptedChatModel([silent] * 5)
    result = _Stage(
        model=model, tools={}, workspace=workspace, events=events, max_empty_turn_nudges=2
    ).run("go")
    assert result.stop_reason == "empty_answer"
    assert len(events.of_type("agent_nudged")) == 2
    assert model.call_count == 3


def test_running_out_of_output_budget_is_reported_distinctly(workspace):
    """'No room left to answer' needs different advice from 'chose to be silent'."""
    truncated = CompletionResult(content="", reasoning="…", finish_reason="length")
    model = ScriptedChatModel([truncated] * 3)
    result = _Stage(model=model, tools={}, workspace=workspace).run("go")
    assert result.stop_reason == "output_budget_exhausted"


def test_a_turn_with_a_prior_draft_is_still_an_answer(workspace):
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    model = ScriptedChatModel(
        [
            calls(tool_call("read_file", path="m.txt"), content="let me look"),
            CompletionResult(content="", finish_reason="stop"),
        ]
    )
    result = _Stage(model=model, tools=catalog_subset("read_file"), workspace=workspace).run("go")
    assert result.stop_reason == "answered"
    assert result.content == "let me look"


def test_is_empty_distinguishes_a_tool_call_from_nothing():
    assert CompletionResult(content="").is_empty
    assert CompletionResult(content="   ").is_empty
    assert not CompletionResult(content="x").is_empty
    assert not CompletionResult(tool_calls=(ToolCall("c", "read_file"),)).is_empty


# -- control-token filtering ---------------------------------------------------------


def test_a_marker_split_across_deltas_is_still_stripped():
    filt = _ControlTokenFilter()
    pieces = ["I need ", "<|", "channel", ">", "to think", "<|end|>", " now"]
    assert "".join(filt.feed(p) for p in pieces) + filt.flush() == "I need to think now"


def test_literal_angle_brackets_in_prose_survive():
    filt = _ControlTokenFilter()
    assert "".join(filt.feed(p) for p in ["if a < b ", "and c ", "> d"]) + filt.flush() == (
        "if a < b and c > d"
    )


def test_an_unterminated_marker_is_released_rather_than_swallowing_everything():
    filt = _ControlTokenFilter()
    text = "start <| " + "x" * 100
    assert "x" * 100 in filt.feed(text) + filt.flush()


def test_the_answer_channel_is_not_filtered(monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"use <|typing|> syntax"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=None: _sse(lines))
    final = list(
        OpenAICompatibleChatModel(
            InferenceSettings(base_url="http://x/v1", model="m")
        ).stream([])
    )[-1].result
    assert final.content == "use <|typing|> syntax"
