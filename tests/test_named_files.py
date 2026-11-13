"""DR-069: Execute starts with the current contents of the files the plan names."""

from __future__ import annotations

from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.inference.types import CompletionResult, Usage
from logic.workflows import WorkflowRequest
from logic.workflows import named_files as named
from logic.workflows.craft import CraftWorkflow
from logic.workflows.named_files import named_files, named_files_section
from tests.support.fakes import RecordingApprovalGate, ScriptedChatModel

CALC = "def divide(a, b):\n    return a / b\n"
TESTS = "from calc import add, divide\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _toy(tmp_path) -> Workspace:
    (tmp_path / "calc.py").write_text(CALC)
    (tmp_path / "test_calc.py").write_text(TESTS)
    return Workspace.at(tmp_path)


def test_only_files_that_exist_are_named_in_the_order_given(tmp_path):
    workspace = _toy(tmp_path)
    text = (
        "Modify `test_calc.py`, e.g. add a case, then `calc.py`. Raise "
        "ValueError. Also see src/calc.py and `calc.py` again."
    )
    assert named_files(text, workspace) == ["test_calc.py", "calc.py"]


def test_a_path_outside_the_workspace_is_never_named(tmp_path):
    inside = tmp_path / "repo"
    inside.mkdir()
    (tmp_path / "secret.txt").write_text("no")
    assert named_files("read ../secret.txt", Workspace.at(inside)) == []


def test_the_section_holds_each_file_s_current_text(tmp_path):
    section = named_files_section("change `calc.py`", _toy(tmp_path))
    assert section.startswith("# Current contents of the files the plan names")
    assert "# calc.py  (lines 1-2 of 2)" in section
    assert "return a / b" in section


def test_nothing_named_means_no_section(tmp_path):
    assert named_files_section("make it faster", _toy(tmp_path)) == ""


def test_a_file_over_the_size_limit_is_left_out_whole(tmp_path, monkeypatch):
    workspace = _toy(tmp_path)
    (tmp_path / "big.py").write_text("x = 1\n" * 50)
    monkeypatch.setattr(named, "MAX_CHARS", 200)
    section = named_files_section("`big.py` and `calc.py`", workspace)
    assert "big.py" not in section
    assert "return a / b" in section


def test_at_most_max_files_are_included(tmp_path, monkeypatch):
    workspace = _toy(tmp_path)
    monkeypatch.setattr(named, "MAX_FILES", 1)
    section = named_files_section("`calc.py` and `test_calc.py`", workspace)
    assert "calc.py  (lines" in section
    assert "test_calc.py" not in section


def _answer(text: str) -> CompletionResult:
    return CompletionResult(
        content=text, finish_reason="stop", usage=Usage(prompt_tokens=1, completion_tokens=1)
    )


def test_only_execute_is_given_the_files_and_reads_them_fresh(tmp_path):
    """Seen on qwen3:8b in four runs of four: Execute's first turn edited
    `src/calc.py` and `test/test_calc.py` with invented text, before reading
    anything, though the plan said `calc.py`."""
    workspace = _toy(tmp_path)
    plan = (
        "Modify `calc.py` so divide raises ValueError.\n\n"
        "## REQUIREMENTS STATUS\n- R1 [Not Started] — divide raises on zero"
    )
    execute = "Did it.\n\n## REQUIREMENTS STATUS\n- R1 [Implemented] — divide raises — Evidence: calc.py:2"
    validate = (
        "Checked.\n\n## REQUIREMENTS STATUS\n- R1 [Verified] — divide raises — "
        "Evidence: ran it\n\nVERDICT: PASS"
    )
    model = ScriptedChatModel([_answer(plan), _answer(execute), _answer(validate)])
    original = model._next

    calls = []

    def edit_during_plan(*args, **kwargs):
        # The file changes after Plan's input was built: Execute must see it
        # as it is when Execute starts, not as it was when Plan began.
        calls.append(1)
        if len(calls) == 1:
            (tmp_path / "calc.py").write_text(CALC + "# changed\n")
        return original(*args, **kwargs)

    model._next = edit_during_plan
    request = WorkflowRequest(
        prompt="make divide() raise ValueError when b is zero",
        workspace=workspace,
        model=model,
        events=RecordingEventSink(),
        approval_gate=RecordingApprovalGate(default=True),
    )
    assert CraftWorkflow().run(request).succeeded

    first_user = [req["messages"][1]["content"] for req in model.requests]
    plan_in, execute_in, validate_in = first_user
    heading = "# Current contents of the files the plan names"
    assert heading not in plan_in
    assert heading not in validate_in
    assert heading in execute_in
    assert "return a / b" in execute_in
    assert "# changed" in execute_in
