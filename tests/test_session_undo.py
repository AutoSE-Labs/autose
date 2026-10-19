"""Reviewing and reverting what a turn wrote, through the session facade (DR-024)."""

from __future__ import annotations

import pytest

from logic.common.approval import ApprovalPolicy
from logic.common.changes import FileChange
from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.persistence import FileSessionStore, SessionRecord, TurnRecord
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


# -- undoing one file of one run (DR-061) -------------------------------------


def _two_file_run(scripted, tmp_path, **kwargs) -> Session:
    """A session whose first run edited a.py and b.py."""
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8", newline="")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted,
        tmp_path,
        [
            calls(
                tool_call(
                    "edit_file",
                    call_id="call_a",
                    path="a.py",
                    edits=[{"old_str": "a = 1", "new_str": "a = 2"}],
                ),
                tool_call(
                    "edit_file",
                    call_id="call_b",
                    path="b.py",
                    edits=[{"old_str": "b = 1", "new_str": "b = 2"}],
                ),
            ),
            answer("Done."),
            answer("Done again."),
        ],
        **kwargs,
    )
    session.run("bump both")
    return session


def _with_change(scripted, tmp_path, change, **kwargs) -> Session:
    """A session resumed with one run that made exactly *change*."""
    session = _session(scripted, tmp_path, [answer("ok")], **kwargs)
    session.restore(
        SessionRecord(
            id="s1",
            turns=(
                TurnRecord(
                    prompt="do it",
                    answer="done",
                    tier=SPARK,
                    files_changed=(change.path,),
                    changes=(change,),
                ),
            ),
        )
    )
    return session


def test_undoing_one_file_leaves_the_runs_other_files_alone(scripted, tmp_path):
    session = _two_file_run(scripted, tmp_path)

    report = session.undo_file(0, "a.py")

    assert report.restored == ("a.py",) and report.skipped == ()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "b = 2\n"
    turn = session.history[0]
    assert [change.path for change in turn.changes] == ["b.py"]
    assert [change.path for change in turn.undone] == ["a.py"]


def test_undoing_a_file_the_run_created_removes_it(scripted, tmp_path):
    # Spark has no write_file, so the created file comes from a stored run.
    (tmp_path / "new.py").write_bytes(b"x = 1\n")
    session = _with_change(
        scripted, tmp_path, FileChange(path="new.py", before=None, after=b"x = 1\n")
    )

    assert session.undo_file(0, "new.py").restored == ("new.py",)
    assert not (tmp_path / "new.py").exists()


def test_undoing_a_file_the_run_deleted_brings_it_back(scripted, tmp_path):
    session = _with_change(
        scripted, tmp_path, FileChange(path="gone.py", before=b"kept = 1\n", after=None)
    )

    assert session.undo_file(0, "gone.py").restored == ("gone.py",)
    assert (tmp_path / "gone.py").read_bytes() == b"kept = 1\n"


def test_a_file_changed_since_is_refused_and_left_as_it_is(scripted, tmp_path):
    session = _two_file_run(scripted, tmp_path)
    (tmp_path / "a.py").write_text("a = 99  # mine\n", encoding="utf-8", newline="")

    report = session.undo_file(0, "a.py")

    assert report.restored == ()
    assert report.skipped == (("a.py", "it has changed since AutoSE wrote it"),)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 99  # mine\n"
    # Still the run's change, still undoable once the file matches again.
    assert [change.path for change in session.history[0].changes] == ["a.py", "b.py"]


def test_the_refusal_names_the_later_run_that_changed_the_file(scripted, tmp_path):
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted,
        tmp_path,
        [
            _edit("m.py", "value = 1", "value = 2"),
            answer("Done."),
            _edit("m.py", "value = 2", "value = 3"),
            answer("Done."),
        ],
    )
    session.run("make it 2")
    session.run("make it 3")

    (path, reason) = session.undo_file(0, "m.py").skipped[0]

    assert path == "m.py" and "run 2" in reason
    assert target.read_text(encoding="utf-8") == "value = 3\n"
    # Undoing the later run's change first is what makes it possible.
    assert session.undo_file(1, "m.py").restored == ("m.py",)
    assert session.undo_file(0, "m.py").restored == ("m.py",)
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_a_change_whose_content_was_not_stored_is_refused_with_its_reason(
    scripted, tmp_path
):
    """A resumed session cannot put back what the store had no room to keep."""
    store = FileSessionStore(tmp_path / ".autose" / "sessions", max_change_bytes=4)
    first = _two_file_run(scripted, tmp_path, store=store)
    second = _session(scripted, tmp_path, [answer("ok")], store=store)
    second.restore(store.load(first.session_id))

    report = second.undo_file(0, "a.py")

    assert report.skipped == (
        ("a.py", "its contents were too large to store with the session"),
    )
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 2\n"


def test_a_whole_run_undo_afterwards_passes_the_undone_file_by(scripted, tmp_path):
    session = _two_file_run(scripted, tmp_path)
    session.undo_file(0, "a.py")
    # Had undo reverted a.py again, this edit would be reported as skipped.
    (tmp_path / "a.py").write_text("a = 7  # mine\n", encoding="utf-8", newline="")

    report = session.undo_last_change()

    assert report is not None
    assert report.restored == ("b.py",) and report.skipped == ()
    assert [change.path for change in session.history[0].undone] == ["a.py", "b.py"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a = 7  # mine\n"


def test_a_file_already_undone_is_refused(scripted, tmp_path):
    session = _two_file_run(scripted, tmp_path)
    session.undo_file(0, "a.py")

    report = session.undo_file(0, "a.py")

    assert report.skipped == (("a.py", "it has already been undone"),)


def test_an_unknown_run_or_file_is_a_lookup_error(scripted, tmp_path):
    session = _two_file_run(scripted, tmp_path)

    with pytest.raises(LookupError, match="no run 5"):
        session.undo_file(4, "a.py")
    with pytest.raises(LookupError, match=r"did not change c\.py"):
        session.undo_file(0, "c.py")


def test_undoing_one_file_is_announced_with_its_path(scripted, tmp_path):
    events = RecordingEventSink()
    session = _two_file_run(scripted, tmp_path, events=events)

    session.undo_file(0, "b.py")

    (event,) = events.of_type("changes_reverted")
    assert event.data["restored"] == ["b.py"] and event.data["turn"] == 0


def test_a_refused_undo_announces_and_saves_nothing(scripted, tmp_path):
    events = RecordingEventSink()
    session = _two_file_run(scripted, tmp_path, events=events)
    (tmp_path / "a.py").write_text("a = 99\n", encoding="utf-8", newline="")

    session.undo_file(0, "a.py")

    assert events.of_type("changes_reverted") == []


def test_undoing_one_file_is_saved_with_the_session(scripted, tmp_path):
    store = FileSessionStore(tmp_path / ".autose" / "sessions")
    session = _two_file_run(scripted, tmp_path, store=store)

    session.undo_file(0, "a.py")

    (turn,) = store.load(session.session_id).turns
    assert [change.path for change in turn.changes] == ["b.py"]
    assert [change.path for change in turn.undone] == ["a.py"]


def test_a_later_run_that_still_holds_the_file_blocks_the_undo(scripted, tmp_path):
    """Order is enforced even when the content happens to match again.

    Run 2 put the file back by hand and run 3 wrote run 1's content again.
    Matching bytes are no licence to undo run 1: putting the file back would
    silently throw away run 3's change, whose record would still claim it.
    """
    target = tmp_path / "m.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    session = _session(
        scripted,
        tmp_path,
        [
            _edit("m.py", "value = 1", "value = 2"),
            answer("Done."),
            _edit("m.py", "value = 2", "value = 1"),
            answer("Done."),
            _edit("m.py", "value = 1", "value = 2"),
            answer("Done."),
        ],
    )
    for prompt in ("make it 2", "put it back", "make it 2 again"):
        session.run(prompt)

    report = session.undo_file(0, "m.py")

    assert report.restored == ()
    assert report.skipped == (
        ("m.py", "run 3 changed it afterwards; undo run 3's change to it first"),
    )
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert [change.path for change in session.history[0].changes] == ["m.py"]
    # Newest first, it goes through.
    for turn in (2, 1, 0):
        assert session.undo_file(turn, "m.py").restored == ("m.py",)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
