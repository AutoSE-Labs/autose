"""Reviewing and reverting what a turn wrote, through the session facade (DR-024)."""

from __future__ import annotations

import pytest

from logic.common.approval import ApprovalPolicy
from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.session import Session
from logic.tiers import SPARK
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call

_MODEL = ModelInfo(name="gemma4:12b", context_length=262144, supports_tools=True)


@pytest.fixture
def scripted(monkeypatch):
    holder: dict = {}

    register_provider(
        Provider(
            name="test-undo",
            chat_model=lambda settings: holder["model"],
            catalog=type("_C", (), {"__init__": lambda s, x: None, "list_models": lambda s: ()}),
            is_local=True,
        ),
        replace=True,
    )
    return holder


def _session(scripted, tmp_path, script, **kwargs) -> Session:
    scripted["model"] = ScriptedChatModel(script)
    kwargs.setdefault("auto_classify", False)
    kwargs.setdefault("default_tier", SPARK)
    return Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_MODEL.name, provider="test-undo"
        ),
        workspace=Workspace.at(tmp_path),
        model_info=_MODEL,
        **kwargs,
    )


def _edit(path: str, old: str, new: str):
    return calls(
        tool_call("edit_file", path=path, edits=[{"old_str": old, "new_str": new}])
    )


# -- a spark turn now reports what it changed --------------------------------


def test_a_spark_edit_is_reported_as_a_changed_file(scripted, tmp_path):
    """Until DR-024, `spark` was the one tier that never said what it touched."""
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )

    turn = session.run("bump the value")

    assert turn.files_changed == ("m.py",)
    assert turn.changed_anything


def test_the_turn_carries_the_content_either_side_of_the_change(scripted, tmp_path):
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )

    (change,) = session.run("bump the value").changes

    assert change.before == b"value = 1\n"
    assert change.after == b"value = 2\n"
    assert "-value = 1" in change.diff()


def test_a_turn_that_changed_nothing_carries_no_changes(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("It is at line 4.")])

    turn = session.run("where is the value set?")

    assert turn.changes == ()
    assert not turn.changed_anything


# -- undo ---------------------------------------------------------------------


def test_undo_puts_the_file_back(scripted, tmp_path):
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )
    session.run("bump the value")

    report = session.undo_last_change()

    assert report is not None and report.restored == ("m.py",)
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_undo_with_nothing_to_undo_returns_none(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("Nothing to do.")])
    session.run("explain this")

    assert session.undo_last_change() is None


def test_undo_targets_the_most_recent_turn_that_wrote(scripted, tmp_path):
    """A question asked after an edit must not make the edit un-undoable."""
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted,
        tmp_path,
        [
            _edit("m.py", "value = 1", "value = 2"),
            answer("Done."),
            answer("It is 2 now."),
        ],
    )
    session.run("bump the value")
    session.run("what is it now?")

    report = session.undo_last_change()

    assert report is not None and report.restored == ("m.py",)
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_undoing_twice_does_not_revert_a_second_turn(scripted, tmp_path):
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )
    session.run("bump the value")
    session.undo_last_change()

    assert session.undo_last_change() is None
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_undo_leaves_a_file_the_user_has_since_edited(scripted, tmp_path):
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )
    session.run("bump the value")
    target.write_text("value = 99  # mine\n", encoding="utf-8", newline="")

    report = session.undo_last_change()

    assert report is not None and report.restored == ()
    assert [path for path, _ in report.skipped] == ["m.py"]
    assert target.read_text(encoding="utf-8") == "value = 99  # mine\n"


def test_last_changes_exposes_the_most_recent_write(scripted, tmp_path):
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )
    session.run("bump the value")

    assert [change.path for change in session.last_changes] == ["m.py"]


def test_a_revert_is_announced_as_an_event(scripted, tmp_path):
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8", newline="")
    events = RecordingEventSink()
    session = _session(
        scripted,
        tmp_path,
        [_edit("m.py", "value = 1", "value = 2"), answer("Done.")],
        events=events,
    )
    session.run("bump the value")
    session.undo_last_change()

    (event,) = events.of_type("changes_reverted")
    assert event.data["restored"] == ["m.py"]


# -- the opt-in approval posture ----------------------------------------------


def test_edits_are_not_gated_by_default(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("ok")])
    assert session.approval_policy.requires("edit_file") is False
    assert session.approval_policy.requires("run_command") is True


def test_a_workspace_may_require_approval_for_edits(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("ok")], require_edit_approval=True)
    assert session.approval_policy.requires("edit_file") is True
    assert session.approval_policy.requires("write_file") is True
    assert session.approval_policy.gates_edits


def test_gating_edits_actually_stops_the_write(scripted, tmp_path):
    """The policy has to reach the executor, not just the session."""
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted,
        tmp_path,
        [_edit("m.py", "value = 1", "value = 2"), answer("It was refused.")],
        require_edit_approval=True,
        approval_gate=None,  # no interface attached: DR-012 fails closed
    )

    turn = session.run("bump the value")

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert turn.files_changed == ()


def test_the_default_policy_matches_the_documented_set():
    assert ApprovalPolicy.default().tools == frozenset(
        {"run_command", "web_search", "fetch_url"}
    )


def test_an_undo_keeps_what_it_reverted_on_the_record(scripted, tmp_path):
    """The run's record still shows the change, as undone, not as if never made."""
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted, tmp_path, [_edit("m.py", "value = 1", "value = 2"), answer("Done.")]
    )
    session.run("bump the value")
    session.undo_last_change()

    turn = session.history[-1]
    assert turn.changes == ()
    assert [change.path for change in turn.undone] == ["m.py"]
    # Nothing is left to undo: undone changes are not reverted twice.
    assert session.undo_last_change() is None
