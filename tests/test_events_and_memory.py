"""Event sinks and the call-scoped ``AgentMemory`` (DR-002 seam, DR-004)."""

from __future__ import annotations

import pytest

from logic.common.events import (
    CallbackEventSink,
    CompositeEventSink,
    Event,
    NullEventSink,
    RecordingEventSink,
)
from logic.common.memory import AgentMemory


def test_a_recording_sink_keeps_and_filters_events():
    sink = RecordingEventSink()
    sink.emit(Event("a"))
    sink.emit(Event("b", "msg", {"k": 1}))
    assert [event.type for event in sink.events] == ["a", "b"]
    assert sink.of_type("b")[0].data == {"k": 1}
    assert sink.to_dicts()[1]["message"] == "msg"


def test_a_null_sink_accepts_and_discards():
    assert NullEventSink().emit(Event("a")) is None


def test_a_broken_adapter_callback_cannot_break_the_run():
    def broken(event):
        raise RuntimeError("renderer crashed")

    CallbackEventSink(broken).emit(Event("a"))


def test_a_composite_sink_fans_out():
    first, second = RecordingEventSink(), RecordingEventSink()
    CompositeEventSink(first, second).emit(Event("a"))
    assert first.events and second.events


def test_agent_memory_seeds_system_then_session_context():
    memory = AgentMemory()
    memory.seed("SYS", [{"role": "user", "content": "earlier"}])
    memory.add_user("now")
    assert [m["content"] for m in memory.messages] == ["SYS", "earlier", "now"]


def test_agent_memory_tracks_rounds_and_tool_calls():
    memory = AgentMemory()
    memory.seed("SYS")
    memory.begin_round()
    memory.add_assistant({"role": "assistant", "content": "thinking"})
    memory.add_tool_result("c1", "result")
    memory.begin_round()
    assert memory.round_count == 2
    assert memory.tool_call_count == 1


def test_agent_memory_remembers_the_last_non_empty_draft():
    memory = AgentMemory()
    memory.seed("SYS")
    memory.add_assistant({"role": "assistant", "content": "draft one"})
    memory.add_assistant({"role": "assistant", "content": None})
    assert memory.last_content == "draft one"


def test_two_agent_memories_share_nothing():
    first, second = AgentMemory(), AgentMemory()
    first.seed("A")
    second.seed("B")
    first.add_user("x")
    assert len(second.messages) == 1


# -- DR-004: request and session scopes ------------------------------------------


def test_request_memory_owns_artifacts_and_last_write_wins():
    from logic.common.memory import RequestMemory

    memory = RequestMemory("do the thing")
    memory.put("plan", "first draft")
    memory.put("plan", "second draft")
    assert memory.get("plan") == "second draft"
    assert memory.has("plan") and not memory.has("execution_summary")


def test_an_oversized_artifact_is_a_stage_failure_not_a_truncation():
    """DR-004: an oversized artifact is a stage that did the wrong thing."""
    from logic.common.memory import ArtifactTooLargeError, RequestMemory

    memory = RequestMemory("x", max_artifact_chars=100)
    with pytest.raises(ArtifactTooLargeError):
        memory.put("plan", "y" * 101)
    assert not memory.has("plan")


def test_a_retried_stage_gets_a_bounded_digest_not_the_discarded_draft():
    from logic.common.memory import RequestMemory

    memory = RequestMemory("x")
    for index in range(6):
        memory.record_attempt("execute", f"reason {index}")
    digest = memory.retry_digest("execute")
    assert "reason 5" in digest
    assert "reason 0" not in digest
    assert len(memory.attempts_for("execute")) <= 3


def test_retry_digests_are_kept_per_stage():
    from logic.common.memory import RequestMemory

    memory = RequestMemory("x")
    memory.record_attempt("execute", "exec problem")
    assert memory.retry_digest("plan") == ""
    assert "exec problem" in memory.retry_digest("execute")


def test_changed_files_are_recorded_once_in_order():
    from logic.common.memory import RequestMemory

    memory = RequestMemory("x")
    memory.note_file_changed("b.py")
    memory.note_file_changed("a.py")
    memory.note_file_changed("b.py")
    assert memory.files_changed == ("b.py", "a.py")


def test_session_memory_keeps_recent_exchanges_verbatim():
    from logic.common.memory import SessionMemory

    memory = SessionMemory(max_verbatim=2, compress=lambda old, new: "summary")
    memory.record("p1", "a1")
    memory.record("p2", "a2")
    assert [e.prompt for e in memory.exchanges] == ["p1", "p2"]
    assert not memory.summary


def test_older_exchanges_roll_into_one_bounded_summary():
    from logic.common.memory import SessionMemory

    memory = SessionMemory(
        max_verbatim=1,
        max_summary_chars=20,
        compress=lambda old, new: (old + new) * 10,
    )
    for index in range(4):
        memory.record(f"p{index}", f"a{index}")
    assert len(memory.exchanges) == 1
    assert 0 < len(memory.summary) <= 20


def test_a_failing_compressor_drops_the_oldest_and_records_it():
    """DR-004: logged, not silent — and never retried indefinitely."""
    from logic.common.memory import SessionMemory

    def broken(old, new):
        raise RuntimeError("summariser unavailable")

    memory = SessionMemory(max_verbatim=1, compress=broken)
    memory.record("p1", "a1")
    memory.record("p2", "a2")
    assert len(memory.exchanges) == 1
    assert memory.compression_failures == 1
    assert memory.dropped == 1
    assert memory.summary == ""


def test_with_no_compressor_overflow_is_dropped_and_counted():
    from logic.common.memory import SessionMemory

    memory = SessionMemory(max_verbatim=1)
    memory.record("p1", "a1")
    memory.record("p2", "a2")
    assert memory.dropped == 1
    assert memory.compression_failures == 0


def test_built_context_is_an_ephemeral_projection():
    from logic.common.memory import SessionMemory

    memory = SessionMemory(max_verbatim=2, compress=lambda old, new: "earlier stuff")
    for index in range(4):
        memory.record(f"p{index}", f"a{index}")
    context = memory.build_context()
    assert context[0]["role"] == "user"
    assert "earlier stuff" in context[0]["content"]
    assert [m["content"] for m in context[1:]] == ["p2", "a2", "p3", "a3"]
    # Building it twice changes nothing — it is a projection, not state.
    assert memory.build_context() == context


def test_clearing_resets_every_counter():
    from logic.common.memory import SessionMemory

    memory = SessionMemory(max_verbatim=1)
    memory.record("p1", "a1")
    memory.record("p2", "a2")
    memory.clear()
    assert memory.exchanges == () and memory.dropped == 0 and memory.summary == ""
