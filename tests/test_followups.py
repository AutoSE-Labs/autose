"""DR-063: what to ask next, worked out from how a run ended.

The rules are a pure function of the finished turn, so each case here builds
one directly: no model, no workspace. What matters as much as each rule is
what is *not* suggested — a vague suggestion, a fourth one, a repeat, or a
decision-record number shown to a person.
"""

from __future__ import annotations

import re

import pytest

from logic.common.changes import FileChange
from logic.followups import MAX_FOLLOW_UPS, FollowUp, suggest_follow_ups
from logic.persistence import TurnRecord
from logic.session import Turn


def _change(path: str) -> FileChange:
    return FileChange(path=path, before=b"value = 1\n", after=b"value = 2\n", tool="edit_file")


def _requirement(ident: str, status: str, description: str, evidence: str = "") -> dict[str, str]:
    return {"id": ident, "status": status, "description": description, "evidence": evidence}


def _turn(**fields) -> Turn:
    fields.setdefault("prompt", "Fix mul and div in ops.py")
    fields.setdefault("answer", "Done.")
    fields.setdefault("tier", "craft")
    return Turn(**fields)


def _labels(turn) -> list[str]:
    return [item.label for item in suggest_follow_ups(turn)]


# -- how the run ended ---------------------------------------------------------


def test_a_failed_check_is_named_with_what_was_wrong():
    turn = _turn(
        stop_reason="attempts_exhausted",
        requirements=(
            _requirement("R1", "Verified", "`mul` multiplies"),
            _requirement("R3", "Failed", "tests cover the zero case", "no test divides by zero"),
        ),
    )
    finish = next(item for item in suggest_follow_ups(turn) if item.label.startswith("Finish"))

    assert finish.label == "Finish R3: tests cover the zero case"
    # The next run plans afresh, so the request carries the requirement and the
    # validator's evidence rather than an ID only this run knew.
    assert "tests cover the zero case" in finish.prompt
    assert "no test divides by zero" in finish.prompt
    assert "R3" not in finish.prompt


def test_a_finished_run_with_an_unchecked_requirement_suggests_finishing_it():
    turn = _turn(
        requirements=(
            _requirement("R1", "Verified", "`mul` multiplies"),
            _requirement("R2", "Implemented", "division by zero raises"),
        )
    )
    assert _labels(turn)[0] == "Finish R2: division by zero raises"


def test_a_stopped_run_leaves_unchecked_requirements_to_carrying_on():
    """Unverified only because it stopped: "carry on" already covers them."""
    turn = _turn(
        stop_reason="cancelled",
        requirements=(_requirement("R1", "Implemented", "`mul` multiplies"),),
    )
    assert _labels(turn) == ["Carry on from where it stopped"]


@pytest.mark.parametrize("reason", ["cancelled", "round_cap", "attempts_exhausted", "time_budget_exhausted"])
def test_a_run_stopped_by_the_person_or_a_limit_offers_to_carry_on(reason):
    item = suggest_follow_ups(_turn(stop_reason=reason))[0]

    assert item.label == "Carry on from where it stopped"
    # A run that did not finish never reaches the model's history, so the
    # request has to say what to carry on with.
    assert item.prompt.endswith("Fix mul and div in ops.py")


def test_a_run_that_failed_offers_to_try_again_saying_what_failed():
    turn = _turn(
        stop_reason="stage_failed",
        route=(
            {"kind": "stage", "stage": "plan", "title": "Plan"},
            {"kind": "stage", "stage": "execute", "title": "Execute"},
        ),
    )
    item = suggest_follow_ups(turn)[0]

    assert item.label == "Try again: the Execute step failed"
    assert item.prompt == "Fix mul and div in ops.py"


def test_an_empty_reply_says_so_in_the_retry():
    turn = _turn(tier="spark", answer="", stop_reason="empty_answer")
    assert _labels(turn) == ["Try again: the model's reply was empty"]


# -- what the run changed --------------------------------------------------------


def test_a_change_with_no_test_beside_it_suggests_one():
    turn = _turn(tier="spark", changes=(_change("src/ops.py"),))
    suggestions = suggest_follow_ups(turn)

    assert [item.label for item in suggestions] == [
        "Add tests for ops.py",
        "Run the tests",
        "Explain what you changed in ops.py",
    ]
    # Short names on the chip, the whole path in the request.
    assert suggestions[0].prompt == "Add tests for src/ops.py."
    assert "src/ops.py" in suggestions[2].prompt


def test_a_change_that_came_with_tests_does_not_ask_for_more():
    turn = _turn(changes=(_change("ops.py"), _change("tests/test_ops.py")))
    assert _labels(turn) == ["Run the tests", "Explain what you changed in ops.py"]


@pytest.mark.parametrize(
    "path",
    ["tests/test_ops.py", "test_ops.py", "ops_test.go", "src/ops.test.ts", "OpsTest.java", "conftest.py"],
)
def test_test_files_are_recognised_by_their_usual_names(path):
    labels = _labels(_turn(changes=(_change("ops.py"), _change(path))))
    assert "Add tests for ops.py" not in labels


def test_a_change_to_a_document_is_explained_but_not_tested():
    assert _labels(_turn(tier="spark", changes=(_change("README.md"),))) == [
        "Explain what you changed in README.md"
    ]


def test_an_undone_change_is_not_suggested_again():
    turn = _turn(tier="spark", changes=(), undone=(_change("ops.py"),))
    assert _labels(turn) == []


def test_half_a_change_is_explained_rather_than_tested():
    turn = _turn(stop_reason="cancelled", changes=(_change("ops.py"),))
    assert _labels(turn) == ["Carry on from where it stopped", "Explain what you changed in ops.py"]


# -- what the answer said ---------------------------------------------------------


def test_an_answer_that_proposes_a_change_offers_to_make_it():
    turn = _turn(
        tier="spark",
        prompt="Why does div multiply?",
        answer="It uses `*`. I suggest you change `a * b` to `a / b` in `ops.py`.",
    )
    assert _labels(turn) == ["Make this change"]


def test_a_diff_in_the_answer_counts_as_a_proposed_change():
    turn = _turn(tier="spark", prompt="Why does div multiply?", answer="```diff\n-a * b\n+a / b\n```")
    assert _labels(turn) == ["Make this change"]


def test_advice_that_names_no_code_is_not_taken_for_a_change():
    turn = _turn(tier="spark", prompt="How do I learn this codebase?", answer="I recommend reading the docs first.")
    assert "Make this change" not in _labels(turn)


def test_an_answer_that_names_a_file_and_a_function_offers_to_go_deeper():
    turn = _turn(
        tier="spark",
        prompt="How is the tier picked?",
        answer="The classifier in `src/logic/classifier.py` asks the model, and `ratchet()` keeps it from dropping.",
    )
    suggestions = suggest_follow_ups(turn)

    assert [item.label for item in suggestions] == [
        "Walk me through classifier.py",
        "Show where ratchet is used",
    ]
    assert "src/logic/classifier.py" in suggestions[0].prompt


def test_an_answer_with_nothing_specific_to_ask_about_gets_no_suggestion():
    """Fewer good suggestions beat a vague one."""
    turn = _turn(tier="spark", prompt="What is 2 + 2?", answer="4. Node.js would agree.")
    assert suggest_follow_ups(turn) == ()


def test_a_file_the_question_already_named_is_not_suggested_back():
    turn = _turn(tier="spark", prompt="What does ops.py do?", answer="`ops.py` has four functions.")
    assert suggest_follow_ups(turn) == ()


# -- the limits on every suggestion -------------------------------------------------


def test_never_more_than_three_and_never_a_repeat():
    turn = _turn(
        requirements=tuple(
            _requirement(f"R{n}", "Implemented", f"requirement number {n}") for n in range(1, 6)
        ),
        changes=(_change("a.py"), _change("b.py")),
    )
    suggestions = suggest_follow_ups(turn)

    assert len(suggestions) == MAX_FOLLOW_UPS
    assert len({item.label for item in suggestions}) == len(suggestions)
    assert all(item.label and item.prompt for item in suggestions)


def test_a_long_requirement_is_shortened_on_the_chip_but_not_in_the_request():
    description = "the parser accepts every separator the ledger format allows and rejects the rest"
    turn = _turn(requirements=(_requirement("R1", "Implemented", description),))
    item = suggest_follow_ups(turn)[0]

    assert len(item.label) <= 56 and item.label.endswith("…")
    assert description in item.prompt


def test_no_label_shows_a_decision_record_number():
    turn = _turn(
        requirements=(_requirement("R1", "Failed", "write DR-063 for the change"),),
        changes=(_change("docs/DecisionRecords/DR-063-x.md"),),
        stop_reason="attempts_exhausted",
    )
    for item in suggest_follow_ups(turn):
        assert not re.search(r"DR-\d", item.label), item


@pytest.mark.parametrize("reason", ["stage_failed", "cancelled"])
def test_the_request_is_never_rewritten(reason):
    """The person's words go back as they wrote them, record numbers and all.

    Their project may name its own records "DR-12": rewriting that to "the
    decision record" would send a different request from the one retried.
    """
    request = "Fix the approval bug described in DR-012 and update docs/DR-012-gate.md"
    item = suggest_follow_ups(_turn(prompt=request, stop_reason=reason))[0]

    assert item.prompt.endswith(request)
    assert "DR-" not in item.label


def test_carrying_on_twice_restates_the_request_once():
    first = suggest_follow_ups(_turn(stop_reason="cancelled"))[0]
    again = suggest_follow_ups(_turn(prompt=first.prompt, stop_reason="round_cap"))[0]

    assert again.prompt == first.prompt
    assert again.prompt.count("Carry on with this request") == 1


def test_a_stored_turn_gets_the_same_suggestions_as_a_live_one():
    """Computed, not stored: a session reloaded from disk loses nothing."""
    live = _turn(tier="spark", changes=(_change("ops.py"),))
    stored = TurnRecord(
        prompt=live.prompt, answer=live.answer, tier=live.tier, changes=live.changes
    )
    assert suggest_follow_ups(stored) == suggest_follow_ups(live)
    assert all(isinstance(item, FollowUp) for item in suggest_follow_ups(stored))


# -- the terminal -------------------------------------------------------------------


def test_the_terminal_names_them_on_one_line_after_the_result():
    import io

    from rich.console import Console

    from tui.activity import ActivityIndicator
    from tui.app import App

    console = Console(file=io.StringIO(), width=120, force_terminal=False, highlight=False)
    app = App(None, console=console, approval_gate=None, activity=ActivityIndicator(console))
    app._render_outcome(_turn(tier="spark", changes=(_change("ops.py"),)))
    last = console.file.getvalue().rstrip().splitlines()[-1]

    assert last.startswith("  next: 1 Add tests for ops.py")
    assert "2 Run the tests" in last and "3 Explain what you changed in ops.py" in last
    assert "/next <n>" in last


class _History:
    """The one part of a session ``/next`` reads."""

    def __init__(self, *turns) -> None:
        self.history = turns


def _terminal(*turns):
    import io

    from rich.console import Console

    from tui.activity import ActivityIndicator
    from tui.app import App

    console = Console(file=io.StringIO(), width=120, force_terminal=False, highlight=False)
    return App(_History(*turns), console=console, approval_gate=None, activity=ActivityIndicator(console))


def test_next_puts_the_whole_request_on_the_line_not_the_label():
    """Typing "Carry on from where it stopped" would mean nothing to the model.

    A stopped run never reaches its history (DR-004), so the terminal hands
    over the request that restates it, as a chip does, and never sends it.
    """
    from tui.commands import dispatch

    stopped = _turn(stop_reason="cancelled", changes=(_change("ops.py"),))
    app = _terminal(stopped)

    assert dispatch(app, "/next") is True
    carried = app._take_carried()
    assert carried.startswith("Carry on with this request")
    assert carried.endswith("Fix mul and div in ops.py")
    assert "\n" not in carried  # the line editor holds one line

    dispatch(app, "/next 2")
    assert app._take_carried() == "Explain what you changed in ops.py, and why."


def test_next_refuses_a_number_it_does_not_have():
    from tui.commands import dispatch

    app = _terminal(_turn(stop_reason="cancelled"))
    dispatch(app, "/next 4")

    assert app._take_carried() == ""
    assert "from 1 to 1" in app.console.file.getvalue()


def test_next_offers_nothing_after_a_run_that_left_no_turn():
    """An error after a good run must not offer that run's suggestions."""
    from logic.errors import AutoSEError
    from tui.commands import dispatch

    app = _terminal(_turn(tier="spark", changes=(_change("ops.py"),)))
    assert app.follow_ups()  # the good run's, until another run is asked for

    def fails(prompt):
        raise AutoSEError("the model went away")

    app.session.stream = fails
    app.session.request_stop = lambda: None
    app._turn("and now this")
    dispatch(app, "/next")

    assert app._take_carried() == ""
    assert "no suggestion" in app.console.file.getvalue()


def test_next_reflects_an_undo_since_the_run_ended():
    from tui.commands import dispatch

    undone = _turn(tier="spark", changes=(), undone=(_change("ops.py"),))
    app = _terminal(undone)
    dispatch(app, "/next")

    assert app._take_carried() == ""


def test_the_terminal_prints_no_hint_when_there_is_nothing_to_suggest():
    from tui.render import follow_ups_line

    assert follow_ups_line(()) is None


def test_carrying_on_again_from_the_terminal_restates_the_request_once():
    """``/next`` puts the request on one line, so the preamble arrives flattened.

    Stopping that run again must still offer the original request with the
    preamble once, not the preamble twice.
    """
    from tui.commands import dispatch

    app = _terminal(_turn(prompt="Fix mul\nand div in ops.py", stop_reason="cancelled"))
    dispatch(app, "/next")
    typed = app._take_carried()
    again = suggest_follow_ups(_turn(prompt=typed, stop_reason="cancelled"))[0]

    assert again.prompt.count("Carry on with this request") == 1
    assert again.prompt.endswith("Fix mul and div in ops.py")
