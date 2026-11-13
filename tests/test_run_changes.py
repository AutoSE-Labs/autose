"""DR-071: Validate gets the run's diff, and short instructions for a small change."""

from __future__ import annotations

from logic.common.changes import ChangeJournal
from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.inference.types import CompletionResult, ToolCall, Usage
from logic.workflows import WorkflowRequest
from logic.workflows import run_changes as rc
from logic.workflows.craft import CraftWorkflow
from logic.workflows.prompts import VALIDATE_PROMPT
from logic.workflows.run_changes import run_changes
from tests.support.fakes import RecordingApprovalGate, ScriptedChatModel
from tests.test_staged_workflow import execute, plan, validate

# -- the diff ---------------------------------------------------------------------


def _journal_with(workspace: Workspace, path: str, before: str, after: str) -> ChangeJournal:
    """A journal holding one write, and the file left as that write left it."""
    journal = ChangeJournal(workspace)
    target = workspace.root / path
    target.write_text(before, encoding="utf-8")

    def write(path: str, content: str) -> str:
        (workspace.root / path).write_text(content, encoding="utf-8")
        return "Wrote it."

    journal.observe(write, tool="write_file")(path, after)
    return journal


def test_no_journal_means_no_section(tmp_path):
    assert run_changes(None, Workspace.at(tmp_path)) == rc.RunChanges()


def test_the_diff_shows_what_the_run_changed(tmp_path):
    workspace = Workspace.at(tmp_path)
    journal = _journal_with(
        workspace, "calc.py", "def divide(a, b):\n    return a / b\n",
        "def divide(a, b):\n    if b == 0:\n        raise ValueError\n    return a / b\n",
    )
    changes = run_changes(journal, workspace)

    assert changes.section.startswith("# What changed in this run")
    assert "+++ b/calc.py" in changes.section
    assert "+    if b == 0:" in changes.section


def test_the_diff_is_against_the_file_as_it_is_now(tmp_path):
    """A command may change a file after the last write; Validate judges disk."""
    workspace = Workspace.at(tmp_path)
    journal = _journal_with(workspace, "a.py", "x = 1\n", "x = 2\n")
    (tmp_path / "a.py").write_text("x = 3\n", encoding="utf-8")

    section = run_changes(journal, workspace).section
    assert "+x = 3" in section
    assert "+x = 2" not in section


def test_a_file_put_back_as_it_was_is_not_shown(tmp_path):
    workspace = Workspace.at(tmp_path)
    journal = _journal_with(workspace, "a.py", "x = 1\n", "x = 2\n")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert run_changes(journal, workspace) == rc.RunChanges()


def test_a_diff_over_the_size_limit_is_named_not_shown(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "MAX_CHARS", 50)
    workspace = Workspace.at(tmp_path)
    journal = _journal_with(workspace, "big.py", "", "x = 1\n" * 20)

    section = run_changes(journal, workspace).section
    assert "```diff" not in section
    assert "too large to show here, so read them yourself: big.py." in section


# -- what Validate is sent ----------------------------------------------------------


def _answer(text: str) -> CompletionResult:
    return CompletionResult(
        content=text, finish_reason="stop", usage=Usage(prompt_tokens=1, completion_tokens=1)
    )


def _edit(path: str, old: str, new: str) -> CompletionResult:
    return CompletionResult(
        tool_calls=(
            ToolCall(
                id="c1",
                name="edit_file",
                arguments={"path": path, "edits": [{"old_str": old, "new_str": new}]},
                raw_arguments="{}",
            ),
        )
    )


def _run(tmp_path, edit: CompletionResult, *, journal=True, overrides=None) -> dict:
    """Run craft once; return the request Validate's first turn was sent."""
    model = ScriptedChatModel(
        [_answer(plan("R1")), edit, _answer(execute("R1")), _answer(validate("R1"))]
    )
    workspace = Workspace.at(tmp_path)
    CraftWorkflow().run(
        WorkflowRequest(
            prompt="make divide() raise ValueError when b is zero",
            workspace=workspace,
            model=model,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
            journal=ChangeJournal(workspace) if journal else None,
            prompt_overrides=overrides or {},
        )
    )
    return model.requests[-1]


def _first_user_message(request: dict) -> str:
    return next(m["content"] for m in request["messages"] if m["role"] == "user")


def _rules(prompt: str) -> str:
    """The stage's own rules: the text after the shared opening (DR-067)."""
    return prompt.split("You are the VALIDATE stage.", 1)[1].strip()[:200]


def test_validate_is_sent_the_diff_and_its_one_prompt(tmp_path):
    (tmp_path / "calc.py").write_text("def divide(a, b):\n    return a / b\n")
    request = _run(tmp_path, _edit("calc.py", "    return a / b", "    if b == 0:\n        raise ValueError\n    return a / b"))
    sent = _first_user_message(request)

    assert "# What changed in this run" in sent
    assert "+    if b == 0:" in sent
    assert _rules(VALIDATE_PROMPT) in sent


def test_a_large_change_gets_the_same_prompt(tmp_path):
    (tmp_path / "big.py").write_text("x = 0\n")
    many = "".join(f"line_{n} = {n}\n" for n in range(40))
    sent = _first_user_message(_run(tmp_path, _edit("big.py", "x = 0\n", many)))

    assert "+line_39 = 39" in sent
    assert _rules(VALIDATE_PROMPT) in sent


def test_without_a_journal_validate_gets_no_diff(tmp_path):
    (tmp_path / "calc.py").write_text("a\n")
    sent = _first_user_message(_run(tmp_path, _edit("calc.py", "a", "b"), journal=False))

    assert "# What changed in this run" not in sent
    assert _rules(VALIDATE_PROMPT) in sent


def test_a_configured_validate_prompt_still_wins_for_a_small_change(tmp_path):
    (tmp_path / "calc.py").write_text("a\n")
    custom = "You are AutoSE in {workspace}. Check it my way."
    request = _run(
        tmp_path, _edit("calc.py", "a", "b"), overrides={"validate": custom}
    )
    system = request["messages"][0]["content"]

    assert "Check it my way." in system
    assert "You are the VALIDATE stage." not in _first_user_message(request)


# -- commands run after the last edit (DR-072) -----------------------------------


def _calls(*calls: tuple[str, dict]) -> CompletionResult:
    return CompletionResult(
        tool_calls=tuple(
            ToolCall(id=f"c{n}", name=name, arguments=arguments, raw_arguments="{}")
            for n, (name, arguments) in enumerate(calls)
        )
    )


def _edit_call(old: str, new: str) -> tuple[str, dict]:
    return "edit_file", {"path": "calc.py", "edits": [{"old_str": old, "new_str": new}]}


def _command(text: str) -> tuple[str, dict]:
    return "run_command", {"command": text}


def test_no_commands_means_no_section():
    assert rc.commands_section(()) == ""


def test_the_section_shows_each_command_and_its_output():
    section = rc.commands_section([("pytest -q", "2 passed\nExit code: 0")])

    assert section.startswith("# Commands run after the last edit")
    assert "$ pytest -q\n2 passed\nExit code: 0" in section


def test_memory_keeps_the_last_commands_and_the_end_of_long_output():
    from logic.common.memory import RequestMemory

    memory = RequestMemory("p")
    for n in range(5):
        memory.note_command(f"cmd {n}", "x" * 5_000 + "Exit code: 0")

    kept = memory.commands_since_change
    assert [command for command, _ in kept] == ["cmd 2", "cmd 3", "cmd 4"]
    assert kept[-1][1].startswith("…")
    assert kept[-1][1].endswith("Exit code: 0")


def test_a_write_forgets_the_commands_run_before_it():
    from logic.common.memory import RequestMemory

    memory = RequestMemory("p")
    memory.note_command("pytest", "1 failed\nExit code: 1")
    memory.note_file_changed("calc.py")

    assert memory.commands_since_change == ()


def test_validate_is_sent_the_test_output_from_after_the_last_edit(tmp_path):
    (tmp_path / "calc.py").write_text("a\n")
    turn = _calls(_edit_call("a", "b"), _command("echo 2 passed"))
    sent = _first_user_message(_run(tmp_path, turn))

    assert "# Commands run after the last edit" in sent
    assert "$ echo 2 passed\n2 passed\n\nExit code: 0" in sent


def test_a_command_followed_by_an_edit_is_not_sent(tmp_path):
    (tmp_path / "calc.py").write_text("a\n")
    turn = _calls(_command("echo 1 failed"), _edit_call("a", "b"))
    sent = _first_user_message(_run(tmp_path, turn))

    assert "# Commands run after the last edit" not in sent
    assert "1 failed" not in sent


def test_a_refused_command_is_not_sent(tmp_path):
    (tmp_path / "calc.py").write_text("a\n")
    turn = _calls(_edit_call("a", "b"), _command("cat /etc/hosts"))
    sent = _first_user_message(_run(tmp_path, turn))

    assert "# Commands run after the last edit" not in sent


def test_the_rules_say_recorded_output_counts_as_a_check():
    rules = " ".join(VALIDATE_PROMPT.split())
    assert "Commands run after the last edit" in rules
    assert "do not run a command again whose output is already there" in rules
