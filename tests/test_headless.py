"""The non-interactive JSON adapter (DR-027).

Doubles as the executable check on DR-002's central claim: a second adapter
over the same `Session` behaves identically, because neither contains
behaviour.
"""

from __future__ import annotations

import io
import json

import pytest

from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call
from tui.headless import EXIT_CONFIGURATION, EXIT_INCOMPLETE, EXIT_OK, run_headless

_MODEL = ModelInfo(name="gemma4:12b", context_length=262144, supports_tools=True)


@pytest.fixture
def endpoint(monkeypatch):
    """A discoverable runtime whose chat model each test scripts."""
    holder: dict = {"model": ScriptedChatModel([answer("hi")])}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-headless",
            chat_model=lambda settings: holder["model"],
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )
    monkeypatch.setenv("AUTOSE_PROVIDER", "test-headless")
    monkeypatch.setenv("AUTOSE_MODEL", _MODEL.name)
    monkeypatch.setenv("AUTOSE_BASE_URL", "http://test/v1")
    # A stray user-level config must not steer the test.
    monkeypatch.setenv("AUTOSE_CONFIG", "")
    monkeypatch.delenv("AUTOSE_CONFIG", raising=False)
    return holder


def _run(endpoint, tmp_path, script, prompt="do the thing", **kwargs):
    endpoint["model"] = ScriptedChatModel(script)
    stream = io.StringIO()
    kwargs.setdefault("tier", "spark")
    code = run_headless(prompt, workspace=tmp_path, stream=stream, **kwargs)
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return code, lines


def _final(lines):
    return lines[-1]


# -- the stream shape ---------------------------------------------------------


def test_every_line_is_one_json_object(endpoint, tmp_path):
    _, lines = _run(endpoint, tmp_path, [answer("done")])
    assert lines and all(isinstance(line, dict) for line in lines)


def test_the_stream_opens_by_naming_the_workspace_and_prompt(endpoint, tmp_path):
    _, lines = _run(endpoint, tmp_path, [answer("done")])

    assert lines[0]["type"] == "started"
    assert lines[0]["prompt"] == "do the thing"
    assert str(tmp_path) in lines[0]["workspace"]


def test_the_last_line_is_the_result(endpoint, tmp_path):
    _, lines = _run(endpoint, tmp_path, [answer("done")])

    result = _final(lines)
    assert result["type"] == "result"
    assert result["ok"] is True
    assert result["answer"] == "done"


def test_logic_events_are_forwarded_as_they_happen(endpoint, tmp_path):
    _, lines = _run(endpoint, tmp_path, [answer("done")])

    kinds = {line.get("event") for line in lines if line["type"] == "event"}
    assert "turn_started" in kinds
    assert "turn_finished" in kinds


def test_the_result_reports_token_usage(endpoint, tmp_path):
    _, lines = _run(
        endpoint, tmp_path, [answer("done", prompt_tokens=12, completion_tokens=3)]
    )

    assert _final(lines)["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
    }


# -- exit codes ----------------------------------------------------------------


def test_a_completed_turn_exits_zero(endpoint, tmp_path):
    code, _ = _run(endpoint, tmp_path, [answer("done")])
    assert code == EXIT_OK


def test_an_empty_prompt_is_a_usage_error(endpoint, tmp_path):
    code, lines = _run(endpoint, tmp_path, [answer("done")], prompt="   ")

    assert code == EXIT_CONFIGURATION
    assert lines[0]["kind"] == "usage"


def test_a_run_that_did_not_complete_exits_non_zero(endpoint, tmp_path):
    """DR-009: a bound being hit is never rendered as success — here, an exit code."""
    from logic.inference.types import CompletionResult

    empty = CompletionResult(content="", finish_reason="length")
    code, lines = _run(endpoint, tmp_path, [empty, empty, empty])

    assert code == EXIT_INCOMPLETE
    assert _final(lines)["ok"] is False
    assert _final(lines)["stop_reason"] != "completed"


def test_sigint_stops_the_run_and_still_writes_a_result_line(endpoint, tmp_path):
    """DR-030: a stopped piped run is still machine-readable, not a torn stream."""
    import signal

    from logic.inference.types import CompletionResult, ToolCall

    (tmp_path / "m.txt").write_text("x", encoding="utf-8")

    class _StopsItself(ScriptedChatModel):
        """Presses ctrl-c on itself after the first round, like an impatient user."""

        def __init__(self, script):
            super().__init__(script)
            self._rounds = 0

        def stream(self, messages, **kwargs):
            self._rounds += 1
            if self._rounds == 1:
                signal.raise_signal(signal.SIGINT)
            return super().stream(messages, **kwargs)

    reading = CompletionResult(
        content="looking",
        tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "m.txt"}),),
    )
    endpoint["model"] = _StopsItself([reading] * 10)
    stream = io.StringIO()
    code = run_headless(
        "read everything",
        workspace=tmp_path,
        stream=stream,
        tier="spark",
        auto_approve=True,
        authorized_by="the test",
    )
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]

    assert code == EXIT_INCOMPLETE
    assert _final(lines)["type"] == "result"
    assert _final(lines)["stop_reason"] == "cancelled"
    assert _final(lines)["ok"] is False
    # The stop itself is on the wire, so a consumer can tell it from a failure.
    assert any(line.get("event") == "stop_requested" for line in lines)


def test_resuming_a_session_that_does_not_exist_is_an_error(endpoint, tmp_path):
    code, lines = _run(endpoint, tmp_path, [answer("done")], resume="nope")

    assert code == EXIT_CONFIGURATION
    assert _final(lines)["kind"] == "no_session"


# -- changes -------------------------------------------------------------------


def test_a_change_is_reported_as_a_diff_not_as_raw_content(endpoint, tmp_path):
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8")
    _, lines = _run(
        endpoint,
        tmp_path,
        [
            calls(
                tool_call(
                    "edit_file",
                    path="m.py",
                    edits=[{"old_str": "value = 1", "new_str": "value = 2"}],
                )
            ),
            answer("done"),
        ],
    )

    (change,) = _final(lines)["changes"]
    assert change["path"] == "m.py"
    assert change["kind"] == "modified"
    assert change["added"] == 1 and change["removed"] == 1
    assert "-value = 1" in change["diff"]
    assert "before" not in change and "after" not in change


def test_files_changed_is_reported_alongside_the_diffs(endpoint, tmp_path):
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8")
    _, lines = _run(
        endpoint,
        tmp_path,
        [
            calls(
                tool_call(
                    "edit_file",
                    path="m.py",
                    edits=[{"old_str": "value = 1", "new_str": "value = 2"}],
                )
            ),
            answer("done"),
        ],
    )

    assert _final(lines)["files_changed"] == ["m.py"]


# -- approval posture -----------------------------------------------------------


def test_an_unattended_run_refuses_a_shell_call_by_default(endpoint, tmp_path):
    """DR-012 fails closed: no approval interface means refusal, not execution."""
    marker = tmp_path / "should-not-exist"
    _, lines = _run(
        endpoint,
        tmp_path,
        [
            calls(tool_call("run_command", command=f"touch {marker.name}")),
            answer("it was refused"),
        ],
    )

    assert not marker.exists()
    resolutions = [
        line for line in lines
        if line["type"] == "event" and line["event"] == "approval_resolved"
    ]
    assert resolutions and resolutions[0]["data"]["approved"] is False


def test_auto_approve_is_recorded_in_the_opening_line(endpoint, tmp_path):
    _, lines = _run(
        endpoint, tmp_path, [answer("done")], auto_approve=True, authorized_by="ci"
    )

    assert lines[0]["auto_approve"] is True


# -- the session it wrote --------------------------------------------------------


def test_a_headless_run_stores_its_session(endpoint, tmp_path):
    _, lines = _run(endpoint, tmp_path, [answer("done")])

    identifier = _final(lines)["session_id"]
    stored = tmp_path / ".autose" / "sessions" / f"{identifier}.json"
    assert stored.is_file()


def test_a_stored_session_can_be_resumed_by_a_later_headless_run(endpoint, tmp_path):
    _, first = _run(endpoint, tmp_path, [answer("the first answer")])
    identifier = _final(first)["session_id"]

    code, second = _run(
        endpoint, tmp_path, [answer("the second answer")], resume=identifier
    )

    assert code == EXIT_OK
    assert _final(second)["session_id"] == identifier
    resumed = [
        line for line in second
        if line["type"] == "event" and line["event"] == "session_resumed"
    ]
    assert resumed


# -- robustness -------------------------------------------------------------------


def test_an_unserialisable_event_does_not_break_the_stream(endpoint, tmp_path, monkeypatch):
    """Every line must stay valid JSON even when an event will not encode."""
    import tui.headless as headless

    stream = io.StringIO()
    write = headless._writer(stream)

    class _Hostile:
        def __repr__(self):
            raise RuntimeError("nope")

    write({"type": "event", "data": {"bad": _Hostile()}})

    line = json.loads(stream.getvalue().strip())
    assert line["type"] == "warning"
