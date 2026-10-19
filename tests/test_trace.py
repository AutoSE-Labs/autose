"""A turn's steps, rounds and stage times are kept with it (DR-055)."""

from __future__ import annotations

import json

import pytest

from logic.common.events import Event
from logic.common.trace import (
    MAX_TRACE_STEPS,
    TRACE_ARGUMENT_CHARS,
    TRACE_PREVIEW_CHARS,
    RunTrace,
    TraceRecorder,
)
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.persistence import FileSessionStore, SessionRecord, TurnRecord
from logic.session import Session
from tests.support.fakes import (
    RecordingApprovalGate,
    ScriptedChatModel,
    answer,
    calls,
    tool_call,
)

_MODEL = ModelInfo(name="gemma4:12b", context_length=262144, supports_tools=True)


def _event(kind: str, message: str = "", at: float = 0.0, **data) -> Event:
    return Event(type=kind, message=message, data=data, timestamp=at)


def _recorder(**kwargs) -> TraceRecorder:
    recorder = TraceRecorder(clock=lambda: 100.0, **kwargs)
    recorder.start()
    return recorder


# -- the recorder -------------------------------------------------------------


def test_a_tool_call_is_kept_with_its_arguments_result_and_timing():
    recorder = _recorder()
    recorder.emit(_event("workflow_started", stages=["plan", "execute"]))
    recorder.emit(_event("stage_started", "Execute", at=101.0, stage="execute", title="Execute", iteration=1))
    recorder.emit(_event("tool_called", tool="read_file", call_id="c1", arguments={"path": "ops.py"}))
    recorder.emit(
        _event(
            "tool_completed",
            tool="read_file",
            call_id="c1",
            preview="def mul(a, b): ...",
            elapsed_ms=12,
            failed=False,
            truncated=False,
            artifact=None,
        )
    )
    recorder.emit(_event("stage_finished", "Execute", at=104.5, stage="execute"))

    trace = recorder.finish()

    assert trace is not None
    tool = trace.steps[0]
    assert tool["type"] == "tool"
    assert tool["tool"] == "read_file"
    assert tool["stage"] == "execute"
    assert tool["args"] == {"path": "ops.py"}
    assert tool["status"] == "done"
    assert tool["elapsed_ms"] == 12
    assert tool["preview"] == "def mul(a, b): ..."
    assert trace.stage_times == ({"stage": "execute", "start": 101.0, "end": 104.5},)
    assert [stage["stage"] for stage in trace.stages] == ["plan", "execute"]
    assert trace.stages[1]["title"] == "Execute"


def test_a_failed_and_a_refused_call_say_so():
    recorder = _recorder()
    recorder.emit(_event("tool_called", tool="run_command", call_id="c1", arguments={"command": "pytest"}))
    recorder.emit(_event("tool_completed", tool="run_command", call_id="c1", preview="Error: boom", failed=True))
    recorder.emit(_event("approval_requested", tool="write_file", arguments={"path": "a.py", "content": "x"}))
    recorder.emit(_event("approval_resolved", tool="write_file", approved=False, reason="not now"))

    trace = recorder.finish()

    failed, refused, note = trace.steps
    assert failed["status"] == "failed"
    assert refused["status"] == "refused"
    assert refused["args"] == {"path": "a.py", "content": "x"}
    assert refused["reason"] == "not now"
    assert note["kind"] == "approval_resolved"
    assert note["approved"] is False


def test_verdicts_send_backs_and_stalls_are_kept_as_notes():
    recorder = _recorder()
    recorder.emit(_event("validation_verdict", "sent back to execute", passed=False, target="execute", unmet=["R2"], feedback="long words"))
    recorder.emit(_event("loopback", "back to execute (1/3)", target="execute", iteration=1, feedback="long words"))
    recorder.emit(_event("agent_stalled", "stopped going in circles", stage="execute"))
    recorder.emit(_event("stage_retry", "empty answer", stage="plan", attempt=2))

    kinds = [(step["kind"], step) for step in recorder.finish().steps]

    assert [kind for kind, _ in kinds] == ["validation_verdict", "loopback", "agent_stalled", "stage_retry"]
    verdict, loopback = kinds[0][1], kinds[1][1]
    assert verdict["unmet"] == ["R2"]
    # The send-back names what the verdict before it found unmet.
    assert loopback["target"] == "execute"
    assert loopback["unmet"] == ["R2"]
    # The validator's own words are reasoning, not a record (DR-004).
    assert "feedback" not in verdict and "feedback" not in loopback
    assert kinds[3][1]["attempt"] == 2


def test_each_model_round_is_kept():
    recorder = _recorder()
    recorder.emit(
        _event(
            "round_usage",
            stage="execute",
            prompt_tokens=900,
            completion_tokens=40,
            estimated_input_tokens=880,
            context_limit=8192,
        )
    )

    assert recorder.finish().rounds == (
        {"stage": "execute", "prompt": 900, "completion": 40, "estimated": 880, "limit": 8192},
    )


def test_long_arguments_and_results_are_trimmed():
    recorder = _recorder()
    recorder.emit(_event("tool_called", tool="write_file", call_id="c1", arguments={"content": "x" * 5000}))
    recorder.emit(_event("tool_completed", tool="write_file", call_id="c1", preview="head " + "y" * 5000 + " tail"))

    step = recorder.finish().steps[0]

    assert len(step["args"]["content"]) <= TRACE_ARGUMENT_CHARS + 1
    assert len(step["preview"]) <= TRACE_PREVIEW_CHARS + 10
    # The start and the end: the end is where a test run says how it went.
    assert step["preview"].startswith("head ")
    assert step["preview"].endswith(" tail")
    assert step["preview_trimmed"] is True


def test_past_the_cap_the_start_and_the_end_are_kept_and_the_rest_counted():
    recorder = _recorder(max_steps=10)
    for number in range(25):
        recorder.emit(_event("agent_nudged", f"nudge {number}", attempt=number))

    trace = recorder.finish()

    assert len(trace.steps) == 10
    assert trace.dropped_steps == 15
    assert trace.dropped_after == 5
    assert [step["attempt"] for step in trace.steps] == [0, 1, 2, 3, 4, 20, 21, 22, 23, 24]


def test_nothing_outside_a_turn_is_recorded():
    recorder = TraceRecorder()
    recorder.emit(_event("stop_requested", "stop"))
    assert recorder.finish() is None

    recorder.start()
    recorder.emit(_event("agent_nudged", "go on"))
    recorder.finish()
    recorder.emit(_event("agent_nudged", "late"))
    recorder.start()
    assert recorder.finish().steps == ()


def test_a_turn_that_ends_mid_call_closes_what_was_open():
    recorder = _recorder()
    recorder.emit(_event("stage_started", at=99.0, stage="execute", iteration=1))
    recorder.emit(_event("tool_called", tool="run_command", call_id="c1", arguments={}))

    trace = recorder.finish()

    assert trace.steps[0]["status"] == "failed"
    assert trace.stage_times[0]["end"] == 100.0


# -- stored form --------------------------------------------------------------


def _sample() -> RunTrace:
    recorder = _recorder()
    recorder.emit(_event("stage_started", at=1.0, stage="execute", title="Execute", iteration=1))
    recorder.emit(_event("tool_called", tool="read_file", call_id="c1", arguments={"path": "a.py"}))
    recorder.emit(_event("tool_completed", tool="read_file", call_id="c1", preview="text", elapsed_ms=3))
    recorder.emit(_event("round_usage", stage="execute", prompt_tokens=10, completion_tokens=2))
    recorder.emit(_event("stage_finished", "Execute", at=2.0, stage="execute", requirements=[{"id": "R1", "status": "Implemented"}]))
    return recorder.finish()


def test_a_trace_round_trips_through_its_stored_form():
    trace = _sample()
    stored = json.loads(json.dumps(trace.to_dict()))

    assert RunTrace.from_dict(stored) == trace


def test_a_stored_trace_keeps_only_known_fields_within_the_caps():
    document = _sample().to_dict()
    document["steps"][0]["secret"] = "not a field"
    document["steps"].append({"type": "something else"})
    document["steps"].extend({"type": "note", "kind": "agent_nudged", "message": "m"} for _ in range(MAX_TRACE_STEPS * 2))

    trace = RunTrace.from_dict(document)

    assert "secret" not in trace.steps[0]
    assert len(trace.steps) == MAX_TRACE_STEPS
    assert trace.dropped_steps == len(document["steps"]) - 1 - MAX_TRACE_STEPS


def test_a_turn_record_carries_its_trace_and_an_old_one_has_none():
    record = TurnRecord(prompt="p", answer="a", tier="craft", trace=_sample())

    assert TurnRecord.from_dict(json.loads(json.dumps(record.to_dict()))).trace == record.trace

    older = record.to_dict()
    del older["trace"]
    assert TurnRecord.from_dict(older).trace is None
    # And one that never had a trace writes none.
    assert "trace" not in TurnRecord(prompt="p", answer="a", tier="craft").to_dict()


def test_a_session_over_its_trace_budget_keeps_the_newest_runs_in_full():
    trace = _sample()
    size = len(json.dumps(trace.to_dict()))
    record = SessionRecord(
        id="s1",
        turns=tuple(TurnRecord(prompt=f"p{n}", answer="a", tier="craft", trace=trace) for n in range(3)),
    )

    bounded = record.bounded(max_trace_bytes=size * 2)

    oldest, middle, newest = (turn.trace for turn in bounded.turns)
    assert newest == trace and middle == trace
    # The oldest keeps its stage times and says what it no longer holds.
    assert oldest.steps == () and oldest.rounds == ()
    assert oldest.stage_times == trace.stage_times
    assert oldest.dropped_steps == len(trace.steps)
    assert oldest.dropped_rounds == len(trace.rounds)


# -- a session keeps it -------------------------------------------------------


@pytest.fixture
def make_session(tmp_path):
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-trace",
            chat_model=lambda settings: holder["model"],
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )

    def build(script, **kwargs) -> Session:
        holder["model"] = ScriptedChatModel(script)
        kwargs.setdefault("auto_classify", False)
        return Session(
            inference=InferenceSettings(base_url="http://test/v1", model=_MODEL.name, provider="test-trace"),
            workspace=Workspace.at(tmp_path),
            model_info=_MODEL,
            **kwargs,
        )

    return build


def test_a_finished_turn_carries_its_trace_and_the_store_keeps_it(make_session, tmp_path):
    (tmp_path / "m.txt").write_text("body text", encoding="utf-8")
    store = FileSessionStore(tmp_path / ".autose" / "sessions")
    session = make_session(
        [
            calls(tool_call("read_file", path="m.txt")),
            calls(tool_call("run_command", call_id="call_1", command="echo hi")),
            answer("done", prompt_tokens=30, completion_tokens=4),
        ],
        store=store,
        approval_gate=RecordingApprovalGate(False),
    )

    turn = session.run("read it")

    assert turn.trace is not None
    tools = [step for step in turn.trace.steps if step["type"] == "tool"]
    assert [(step["tool"], step["status"]) for step in tools] == [
        ("read_file", "done"),
        ("run_command", "refused"),
    ]
    assert tools[0]["args"] == {"path": "m.txt"}
    assert "body text" in tools[0]["preview"]
    assert turn.trace.rounds
    assert turn.trace.finished_at >= turn.trace.started_at > 0

    stored = store.load(session.session_id).turns[0].trace
    assert stored == turn.trace

    # Resuming keeps it, so a reopened session can still show its steps.
    resumed = make_session([answer("x")], store=store)
    resumed.restore(store.load(session.session_id))
    assert resumed.history[0].trace == turn.trace


def test_each_turn_has_its_own_trace(make_session, tmp_path):
    (tmp_path / "m.txt").write_text("body", encoding="utf-8")
    session = make_session([calls(tool_call("read_file", path="m.txt")), answer("one"), answer("two")])

    first = session.run("first")
    second = session.run("second")

    assert any(step["type"] == "tool" for step in first.trace.steps)
    assert not any(step["type"] == "tool" for step in second.trace.steps)


def test_a_session_stored_before_traces_still_loads(make_session, tmp_path):
    store = FileSessionStore(tmp_path / ".autose" / "sessions")
    store.root.mkdir(parents=True)
    (store.root / "old.json").write_text(
        json.dumps(
            {
                "version": 1,
                "id": "old",
                "turns": [{"prompt": "p", "answer": "a", "tier": "spark", "route": []}],
            }
        ),
        encoding="utf-8",
    )

    record = store.load("old")
    session = make_session([answer("x")])
    session.restore(record)

    assert record.turns[0].trace is None
    assert session.history[0].trace is None
