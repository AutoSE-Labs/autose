"""Rendering and undoing a turn's changes in the terminal (DR-024).

The Adapter's job here is presentation only: every decision — what a change
is, whether it can be reverted, whether reverting is safe — belongs to
``logic`` and is tested there. What these check is that the terminal shows it
and calls it.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from logic.common.approval import ApprovalRequest
from logic.common.changes import ChangeJournal, FileChange, RevertReport
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.session import Session
from tests.support.fakes import ScriptedChatModel, answer, calls, tool_call
from tui.activity import ActivityIndicator
from tui.approval import TerminalApprovalGate
from tui.commands import command_help, dispatch
from tui.render import approval_panel, changes_summary, diff_panel, revert_report

_MODEL = ModelInfo(name="gemma4:12b", context_length=262144, supports_tools=True)


def _console() -> Console:
    return Console(file=io.StringIO(), width=100, force_terminal=False, highlight=False)


def _render(renderable) -> str:
    console = _console()
    console.print(renderable)
    return console.file.getvalue()


@pytest.fixture
def make_session(tmp_path):
    """Build a session around a scripted model.

    A factory rather than a fixture value because ``Session`` builds its chat
    model in its constructor, so the script has to be decided before it exists.
    """
    holder: dict = {}

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL,)

    register_provider(
        Provider(
            name="test-tui-changes",
            chat_model=lambda settings: holder["model"],
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )

    def build(script) -> Session:
        holder["model"] = ScriptedChatModel(script)
        return Session(
            inference=InferenceSettings(
                base_url="http://test/v1",
                model=_MODEL.name,
                provider="test-tui-changes",
            ),
            workspace=Workspace.at(tmp_path),
            model_info=_MODEL,
            auto_classify=False,
        )

    return build


class _App:
    def __init__(self, session, console):
        self.session = session
        self.console = console
        self.approval_gate = TerminalApprovalGate(console)
        self.activity = ActivityIndicator(console)


@pytest.fixture
def app(make_session):
    return _App(make_session([answer("nothing to do")]), _console())


@pytest.fixture
def app_that_edited(make_session, tmp_path):
    """An app whose session has already run one turn that edited a file."""
    (tmp_path / "m.py").write_text("value = 1\n", encoding="utf-8")
    session = make_session(
        [
            calls(
                tool_call(
                    "edit_file",
                    path="m.py",
                    edits=[{"old_str": "value = 1", "new_str": "value = 2"}],
                )
            ),
            answer("Done."),
        ]
    )
    session.run("bump it")
    return _App(session, _console())


# -- the summary --------------------------------------------------------------


def test_the_summary_names_the_file_and_the_size_of_the_change():
    changes = (FileChange(path="m.py", before=b"a\n", after=b"b\nc\n"),)
    rendered = changes_summary(changes)
    text = _render(rendered)

    assert "m.py" in text
    assert "+2" in text
    assert "1" in text
    assert "changed" in text


def test_the_summary_distinguishes_a_new_file():
    changes = (FileChange(path="fresh.py", before=None, after=b"x\n"),)
    assert "new" in _render(changes_summary(changes))


def test_the_summary_says_how_to_review_and_undo():
    text = _render(changes_summary((FileChange(path="m.py", before=b"a", after=b"b"),)))
    assert "/diff" in text and "/undo" in text
    assert "shell" in text


def test_the_diff_says_shell_changes_are_not_restored():
    text = _render(diff_panel((FileChange(path="m.py", before=b"a\n", after=b"b\n"),)))
    assert "shell" in text
    assert "not restored" in text


def test_a_change_too_large_to_record_is_labelled_rather_than_counted():
    change = FileChange(
        path="big.bin", before=None, after=None, restorable=False, reason="too large"
    )
    assert "unrecorded" in _render(changes_summary((change,)))


# -- the diff -----------------------------------------------------------------


def test_the_diff_panel_shows_both_sides():
    changes = (FileChange(path="m.py", before=b"one\n", after=b"two\n"),)
    text = _render(diff_panel(changes))

    assert "-one" in text
    assert "+two" in text


def test_the_diff_panel_titles_itself_with_the_totals():
    changes = (FileChange(path="m.py", before=b"a\n", after=b"b\nc\n"),)
    text = _render(diff_panel(changes))
    assert "1 file" in text


def test_an_empty_diff_says_so_rather_than_rendering_a_blank_panel():
    assert "nothing has been changed" in _render(diff_panel(()))


# -- the revert report ---------------------------------------------------------


def test_the_report_names_what_was_restored():
    text = _render(revert_report(RevertReport(restored=("m.py",))))
    assert "m.py" in text and "restored" in text


def test_the_report_explains_what_it_would_not_touch():
    report = RevertReport(skipped=(("m.py", "it has changed since AutoSE wrote it"),))
    text = _render(revert_report(report))

    assert "left alone" in text
    assert "changed since AutoSE wrote it" in text


def test_an_empty_report_says_there_was_nothing_to_undo():
    assert "nothing to undo" in _render(revert_report(RevertReport()))


# -- the commands ---------------------------------------------------------------


def test_the_new_commands_are_listed_in_help():
    listing = command_help()
    assert "/diff" in listing
    assert "/undo" in listing
    assert "/changes" in listing


def test_diff_reviews_what_the_session_wrote(app_that_edited):
    app = app_that_edited

    dispatch(app, "/diff")

    output = app.console.file.getvalue()
    assert "-value = 1" in output
    assert "+value = 2" in output


def test_changes_lists_without_the_diff(app_that_edited):
    app = app_that_edited

    dispatch(app, "/changes")

    output = app.console.file.getvalue()
    assert "m.py" in output
    assert "-value = 1" not in output


def test_undo_restores_the_file_and_says_so(app_that_edited, tmp_path):
    app = app_that_edited

    dispatch(app, "/undo")

    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "restored" in app.console.file.getvalue()


def test_undo_with_nothing_to_undo_is_not_an_error(app):
    assert dispatch(app, "/undo") is True
    assert "nothing to undo" in app.console.file.getvalue()


def test_diff_with_nothing_written_says_so(app):
    dispatch(app, "/diff")
    assert "nothing has been changed" in app.console.file.getvalue()


def test_undo_warns_when_it_left_a_file_alone(app_that_edited, tmp_path):
    app = app_that_edited
    (tmp_path / "m.py").write_text("mine now\n", encoding="utf-8")

    dispatch(app, "/undo")

    output = app.console.file.getvalue()
    assert "left alone" in output
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == "mine now\n"


# -- the approval panel for a gated edit ----------------------------------------


def test_a_gated_edit_shows_what_it_would_replace():
    request = ApprovalRequest(
        tool_name="edit_file",
        arguments={
            "path": "src/thing.py",
            "edits": [{"old_str": "timeout = 5", "new_str": "timeout = 30"}],
        },
    )
    text = _render(approval_panel(request))

    assert "src/thing.py" in text
    assert "- timeout = 5" in text
    assert "+ timeout = 30" in text


def test_a_gated_write_shows_the_content_it_would_land():
    request = ApprovalRequest(
        tool_name="write_file",
        arguments={"path": "new.py", "content": "print('hello')\n"},
    )
    text = _render(approval_panel(request))

    assert "new.py" in text
    assert "+ print('hello')" in text


def test_a_long_proposed_write_is_truncated_rather_than_flooding_the_prompt():
    content = "\n".join(f"line {index}" for index in range(50))
    request = ApprovalRequest(
        tool_name="write_file", arguments={"path": "big.py", "content": content}
    )
    text = _render(approval_panel(request))

    assert "more line(s)" in text
    assert "line 49" not in text


def test_a_shell_call_is_unaffected_by_the_edit_preview():
    request = ApprovalRequest(tool_name="run_command", arguments={"command": "pytest -q"})
    text = _render(approval_panel(request))

    assert "pytest -q" in text


def test_the_journal_and_the_renderer_agree_on_a_real_edit(tmp_path):
    """End to end: a real write, journalled, rendered."""
    workspace = Workspace.at(tmp_path)
    (tmp_path / "m.py").write_text("one\n", encoding="utf-8")
    journal = ChangeJournal(workspace)
    from logic.common.tools.filesystem import write_file

    journal.observe(write_file, tool="write_file")("m.py", "two\n", workspace=workspace)

    assert "+two" in _render(diff_panel(journal.changes))


# -- the Pro-only knowledge surface is absent (DR-032) ---------------------------


def test_knowledge_is_not_a_command_in_the_free_edition(app):
    assert "/knowledge" not in command_help()
    dispatch(app, "/knowledge")
    assert "unknown command" in app.console.file.getvalue().lower()


def test_undo_points_at_an_earlier_session_that_still_has_changes(app, tmp_path):
    """A fresh session must not read as though the record had been lost."""
    from logic.common.changes import FileChange
    from logic.persistence import FileSessionStore, SessionRecord, TurnRecord

    store = FileSessionStore(tmp_path / ".autose" / "sessions")
    store.save(
        SessionRecord(
            id="20260830-120000-aaaaaa",
            turns=(
                TurnRecord(
                    prompt="earlier work",
                    answer="done",
                    tier="spark",
                    changes=(FileChange(path="m.py", before=b"a\n", after=b"b\n"),),
                ),
            ),
        )
    )
    app.session._store = store

    dispatch(app, "/undo")

    output = app.console.file.getvalue()
    assert "there is nothing to undo" in output
    assert "20260830-120000-aaaaaa" in output
    assert "--resume" in output


def test_no_hint_when_there_is_no_earlier_session(app):
    dispatch(app, "/undo")
    assert "--resume" not in app.console.file.getvalue()
