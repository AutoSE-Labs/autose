"""The workspace change journal and undo (DR-024)."""

from __future__ import annotations

import pytest

from logic.common.changes import (
    ChangeJournal,
    FileChange,
    track_changes,
)
from logic.common.tools.editing import edit_file
from logic.common.tools.filesystem import write_file


@pytest.fixture
def journal(workspace) -> ChangeJournal:
    return ChangeJournal(workspace)


def _write(journal, workspace):
    return journal.observe(write_file, tool="write_file")


def _edit(journal, workspace):
    return journal.observe(edit_file, tool="edit_file")


# -- recording ---------------------------------------------------------------


def test_a_new_file_is_recorded_as_created(journal, workspace):
    _write(journal, workspace)("fresh.py", "print(1)\n", workspace=workspace)

    (change,) = journal.changes
    assert change.path == "fresh.py"
    assert change.kind == "created"
    assert change.before is None
    assert change.after == b"print(1)\n"


def test_an_existing_file_is_recorded_as_modified(journal, workspace):
    (workspace.root / "m.py").write_text("one\n", encoding="utf-8", newline="")

    _write(journal, workspace)("m.py", "two\n", workspace=workspace)

    (change,) = journal.changes
    assert change.kind == "modified"
    assert change.before == b"one\n"
    assert change.after == b"two\n"


def test_a_refused_write_is_not_a_change(journal, workspace):
    """A handler that returned an error changed nothing, so nothing is recorded."""

    def refusing(path, *args, **kwargs):
        return "Error: the design stage may only write decision records."

    journal.observe(refusing, tool="write_file")("src/thing.py", "x", workspace=workspace)

    assert journal.changes == ()


def test_a_write_that_changes_nothing_is_not_a_change(journal, workspace):
    (workspace.root / "same.py").write_text("one\n", encoding="utf-8", newline="")

    _write(journal, workspace)("same.py", "one\n", workspace=workspace)

    assert journal.changes == ()


def test_repeated_edits_to_one_file_coalesce_to_the_net_change(journal, workspace):
    """The *before* is where the turn started, not the previous intermediate."""
    (workspace.root / "m.py").write_text("one\n", encoding="utf-8", newline="")
    handler = _write(journal, workspace)

    handler("m.py", "two\n", workspace=workspace)
    handler("m.py", "three\n", workspace=workspace)
    handler("m.py", "four\n", workspace=workspace)

    (change,) = journal.changes
    assert change.before == b"one\n"
    assert change.after == b"four\n"


def test_editing_a_file_back_to_where_it_started_leaves_no_change(journal, workspace):
    (workspace.root / "m.py").write_text("one\n", encoding="utf-8", newline="")
    handler = _write(journal, workspace)

    handler("m.py", "two\n", workspace=workspace)
    handler("m.py", "one\n", workspace=workspace)

    assert journal.changes == ()


def test_a_path_escaping_the_workspace_is_never_journalled(journal, workspace):
    _write(journal, workspace)("../outside.py", "x", workspace=workspace)
    assert journal.changes == ()


def test_the_tracked_file_count_is_bounded(workspace):
    journal = ChangeJournal(workspace, max_tracked_files=2)
    handler = journal.observe(write_file, tool="write_file")

    for index in range(5):
        handler(f"f{index}.py", "x\n", workspace=workspace)

    assert len(journal) == 2
    assert journal.overflowed == 3


def test_a_file_too_large_to_snapshot_is_reported_but_not_restorable(workspace):
    journal = ChangeJournal(workspace, max_snapshot_bytes=8)
    (workspace.root / "big.py").write_text("x" * 100, encoding="utf-8", newline="")

    journal.observe(write_file, tool="write_file")(
        "big.py", "y" * 100, workspace=workspace
    )

    (change,) = journal.changes
    assert change.restorable is False
    assert "too large" in change.reason or "KiB" in change.reason


# -- diffing -----------------------------------------------------------------


def test_the_diff_shows_what_actually_changed(journal, workspace):
    (workspace.root / "m.py").write_text("one\ntwo\nthree\n", encoding="utf-8", newline="")

    _edit(journal, workspace)(
        "m.py", [{"old_str": "two", "new_str": "TWO"}], workspace=workspace
    )

    diff = journal.diff()
    assert "-two" in diff
    assert "+TWO" in diff
    assert "a/m.py" in diff and "b/m.py" in diff


def test_a_created_file_diffs_against_nothing(journal, workspace):
    _write(journal, workspace)("fresh.py", "hello\n", workspace=workspace)

    diff = journal.diff()
    assert "/dev/null" in diff
    assert "+hello" in diff


def test_line_statistics_count_both_directions(journal, workspace):
    (workspace.root / "m.py").write_text("a\nb\nc\n", encoding="utf-8", newline="")

    _write(journal, workspace)("m.py", "a\nB\nc\nd\n", workspace=workspace)

    assert journal.stats() == (2, 1)


def test_a_binary_change_is_named_rather_than_diffed(journal, workspace):
    target = workspace.root / "blob.bin"
    target.write_bytes(b"\x00\x01\x02")
    journal.record("blob.bin", before=b"\x00\x01", after=b"\x00\x02", tool="write_file")

    change = journal.changes[0]
    assert change.is_text is False
    assert "binary file" in change.diff()
    assert change.stats == (0, 0)


# -- undo ---------------------------------------------------------------------


def test_undo_restores_a_modified_file_exactly(journal, workspace):
    target = workspace.root / "m.py"
    target.write_text("original\n", encoding="utf-8", newline="")

    _write(journal, workspace)("m.py", "replaced\n", workspace=workspace)
    report = journal.revert()

    assert report.restored == ("m.py",)
    assert report.complete
    assert target.read_text(encoding="utf-8") == "original\n"


def test_undo_deletes_a_file_the_run_created(journal, workspace):
    _write(journal, workspace)("fresh.py", "hello\n", workspace=workspace)

    journal.revert()

    assert not (workspace.root / "fresh.py").exists()


def test_undo_preserves_bytes_that_are_not_valid_utf8(journal, workspace):
    target = workspace.root / "blob.bin"
    target.write_bytes(b"\xff\xfe\x00binary")
    journal.record(
        "blob.bin",
        before=b"\xff\xfe\x00binary",
        after=b"replaced",
        tool="write_file",
    )
    target.write_bytes(b"replaced")

    journal.revert()

    assert target.read_bytes() == b"\xff\xfe\x00binary"


def test_undo_refuses_a_file_that_changed_since_autose_wrote_it(journal, workspace):
    """The safety property: never destroy work AutoSE did not make."""
    target = workspace.root / "m.py"
    target.write_text("original\n", encoding="utf-8", newline="")
    _write(journal, workspace)("m.py", "autose wrote this\n", workspace=workspace)

    target.write_text("the user edited it afterwards\n", encoding="utf-8", newline="")
    report = journal.revert()

    assert report.restored == ()
    assert report.skipped == (("m.py", "it has changed since AutoSE wrote it"),)
    assert target.read_text(encoding="utf-8") == "the user edited it afterwards\n"


def test_undo_will_not_restore_a_file_it_could_not_snapshot(workspace):
    journal = ChangeJournal(workspace, max_snapshot_bytes=8)
    (workspace.root / "big.py").write_text("x" * 100, encoding="utf-8", newline="")
    journal.observe(write_file, tool="write_file")(
        "big.py", "y" * 100, workspace=workspace
    )

    report = journal.revert()

    assert report.restored == ()
    assert len(report.skipped) == 1
    assert (workspace.root / "big.py").read_text(encoding="utf-8") == "y" * 100


def test_a_partial_undo_keeps_what_it_could_not_restore(journal, workspace):
    ok = workspace.root / "ok.py"
    moved = workspace.root / "moved.py"
    ok.write_text("a\n", encoding="utf-8", newline="")
    moved.write_text("b\n", encoding="utf-8", newline="")
    handler = _write(journal, workspace)
    handler("ok.py", "a2\n", workspace=workspace)
    handler("moved.py", "b2\n", workspace=workspace)
    moved.write_text("someone else\n", encoding="utf-8", newline="")

    report = journal.revert()

    assert report.restored == ("ok.py",)
    assert [path for path, _ in report.skipped] == ["moved.py"]
    assert not report.complete
    # What was restored is dropped; what was not stays available to retry.
    assert journal.paths == ("moved.py",)


def test_a_journal_can_be_rebuilt_from_the_changes_a_turn_carried(workspace):
    """What makes undo work after the run that produced it has ended."""
    target = workspace.root / "m.py"
    target.write_text("original\n", encoding="utf-8", newline="")
    live = ChangeJournal(workspace)
    live.observe(write_file, tool="write_file")("m.py", "new\n", workspace=workspace)

    rebuilt = ChangeJournal.from_changes(workspace, live.changes)
    report = rebuilt.revert()

    assert report.restored == ("m.py",)
    assert target.read_text(encoding="utf-8") == "original\n"


def test_reverting_nothing_reports_nothing(journal):
    report = journal.revert()
    assert report.empty
    assert not report.complete


# -- instrumentation ----------------------------------------------------------


class _Agent:
    """The slice of ``BaseAgent`` that ``track_changes`` actually uses."""

    def __init__(self, handlers: dict) -> None:
        self.handlers = handlers

    def has_tool(self, name: str) -> bool:
        return name in self.handlers

    def instrument(self, name: str, wrap) -> None:
        self.handlers[name] = wrap(self.handlers[name])


def test_tracking_composes_over_a_handler_rather_than_replacing_it(workspace):
    """DR-018's confinement must survive being journalled (DR-008)."""
    refused: list[str] = []

    def confined(path, *args, **kwargs):
        if not path.startswith("docs/"):
            refused.append(path)
            return "Error: outside the records directory."
        return write_file(path, *args, **kwargs)

    agent = _Agent({"write_file": confined})
    journal = ChangeJournal(workspace)
    seen: list[str] = []
    track_changes(agent, journal, on_change=seen.append)

    agent.handlers["write_file"]("src/thing.py", "x", workspace=workspace)
    agent.handlers["write_file"]("docs/note.md", "y", workspace=workspace)

    assert refused == ["src/thing.py"], "the underlying guard stopped running"
    assert journal.paths == ("docs/note.md",)
    assert seen == ["docs/note.md"]


def test_tracking_a_tool_the_agent_does_not_have_is_a_no_op(workspace):
    agent = _Agent({"read_file": lambda path, **kwargs: "contents"})
    track_changes(agent, ChangeJournal(workspace))
    assert set(agent.handlers) == {"read_file"}


def test_a_none_journal_is_a_no_op(workspace):
    agent = _Agent({"write_file": write_file})
    original = agent.handlers["write_file"]
    track_changes(agent, None)
    assert agent.handlers["write_file"] is original


def test_a_change_carries_which_tool_made_it(journal, workspace):
    (workspace.root / "m.py").write_text("one\n", encoding="utf-8", newline="")
    _edit(journal, workspace)(
        "m.py", [{"old_str": "one", "new_str": "two"}], workspace=workspace
    )
    assert journal.changes[0].tool == "edit_file"


def test_file_change_is_a_value_that_can_be_compared(journal):
    left = FileChange(path="a.py", before=b"1", after=b"2")
    right = FileChange(path="a.py", before=b"1", after=b"2")
    assert left == right
