"""Suggested follow-ups in the dashboard (DR-063).

The rules are logic's and are tested in ``test_followups``. What is checked
here is the seam: every turn the dashboard is sent carries its suggestions,
worked out rather than stored, never more than three; and the page shows them
above the request box, where a chip fills the box and never sends.
"""

from __future__ import annotations

import pytest

gui = pytest.importorskip("gui", reason="the GUI adapter is a Pro-edition build")

from gui.payloads import turn_payload  # noqa: E402
from gui.server import STATIC_ROOT  # noqa: E402
from logic.common.changes import FileChange  # noqa: E402
from logic.session import Turn  # noqa: E402
from tests import test_gui  # noqa: E402
from tests.support.fakes import answer, calls, tool_call  # noqa: E402

#: The provider `test_gui` registers, so a bridge here runs a scripted model.
scripted = test_gui.scripted


def test_a_finished_turn_reaches_the_page_with_what_to_ask_next(scripted, tmp_path):
    (tmp_path / "ops.py").write_text("def mul(a, b):\n    return a / b\n")
    bridge = test_gui._bridge(
        scripted,
        tmp_path,
        [
            calls(
                tool_call(
                    "edit_file",
                    path="ops.py",
                    edits=[{"old_str": "a / b", "new_str": "a * b"}],
                )
            ),
            answer("mul multiplies now"),
        ],
    )
    subscriber = bridge.subscribe()
    bridge.submit("fix mul")
    frame = test_gui._drain(subscriber, "turn")

    expected = ["Add tests for ops.py", "Run the tests", "Explain what you changed in ops.py"]
    assert [item["label"] for item in frame["turn"]["follow_ups"]] == expected
    assert frame["turn"]["follow_ups"][0]["prompt"] == "Add tests for ops.py."
    # The history a reloaded page reads carries the same, worked out again.
    history = bridge.snapshot()["history"]
    assert [item["label"] for item in history[-1]["follow_ups"]] == expected


def test_a_turn_with_nothing_specific_to_suggest_sends_an_empty_list(scripted, tmp_path):
    bridge = test_gui._bridge(scripted, tmp_path, [answer("the answer is 4")])
    subscriber = bridge.subscribe()
    bridge.submit("what is 2 + 2?")

    assert test_gui._drain(subscriber, "turn")["turn"]["follow_ups"] == []


def test_the_payload_never_carries_more_than_three():
    turn = Turn(
        prompt="tidy up",
        answer="Done.",
        tier="craft",
        requirements=tuple(
            {"id": f"R{n}", "status": "Failed", "description": f"check {n}", "evidence": ""}
            for n in range(1, 7)
        ),
        stop_reason="attempts_exhausted",
        changes=(FileChange(path="a.py", before=b"1\n", after=b"2\n"),),
    )
    follow_ups = turn_payload(turn, index=0)["follow_ups"]

    assert len(follow_ups) == 3
    assert all(set(item) == {"label", "prompt"} for item in follow_ups)
    assert all(item["label"] and item["prompt"] for item in follow_ups)


def test_the_page_shows_them_above_the_request_box_and_a_chip_never_sends():
    page = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    area = page.split('class="composer-area"', 1)[1]

    assert area.index('id="follow-ups"') < area.index('id="composer"')
    assert "turn.follow_ups" in script
    handler = script.split("followUpsBox.addEventListener(\"click\"", 1)[1].split("\n  });", 1)[0]
    assert "dom.prompt.value = item.prompt" in handler
    assert "submitPrompt" not in handler and "/api/prompt" not in handler
    # Typing puts them away; so does a run starting, which repaints the
    # conversation and with it the suggestions.
    assert 'dom.prompt.addEventListener("input", renderFollowUps)' in script
    conversation = script.split("function renderConversation()", 1)[1].split("\n}\n", 1)[0]
    assert "renderFollowUps()" in conversation
