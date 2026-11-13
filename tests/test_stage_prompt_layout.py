"""DR-067: every stage sends the same system message, and its own rules first in
the user message, so a local runtime reuses what it read for the last stage."""

from __future__ import annotations

from logic.workflows.prompts import PLAN_PROMPT
from logic.workflows.stages import EXECUTE, PLAN, STAGES, VALIDATE, StageAgent
from tests.support.fakes import ScriptedChatModel, answer


def _sent(workspace, stage: str, *, template: str | None = None) -> list[dict]:
    model = ScriptedChatModel([answer("done")])
    StageAgent(
        STAGES[stage],
        workspace=workspace,
        model=model,
        system_prompt_template=template,
    ).run("# Request\n\nmake divide() raise ValueError")
    return model.requests[0]["messages"]


def test_every_stage_sends_the_same_system_message(workspace):
    systems = {_sent(workspace, s)[0]["content"] for s in (PLAN, EXECUTE, VALIDATE)}
    assert len(systems) == 1
    assert "You are AutoSE" in systems.pop()


def test_a_stage_s_own_rules_lead_its_first_user_message(workspace):
    messages = _sent(workspace, VALIDATE)
    system, first_user = messages[0]["content"], messages[1]["content"]
    assert "VALIDATE stage" not in system
    assert first_user.startswith("You are the VALIDATE stage.")
    assert first_user.index("VALIDATE stage") < first_user.index("# Request")


def test_plan_s_tools_are_an_exact_prefix_of_every_later_stage_s(workspace):
    """The tool list follows the system message, so it must match too."""
    def schema(stage):
        model = ScriptedChatModel([answer("done")])
        StageAgent(STAGES[stage], workspace=workspace, model=model).run("x")
        return model.requests[0]["tools"]

    plan = schema(PLAN)
    for later in (EXECUTE, VALIDATE):
        assert schema(later)[: len(plan)] == plan


def test_a_configured_prompt_without_the_shared_opening_is_sent_whole(workspace):
    """DR-021: a hand-written prompt keeps working, as the system message."""
    messages = _sent(workspace, PLAN, template="Plan carefully. Workspace: {workspace}")
    assert messages[0]["content"].startswith("Plan carefully.")
    assert messages[1]["content"] == "# Request\n\nmake divide() raise ValueError"


def test_a_configured_prompt_with_the_shared_opening_is_split_like_the_default(
    workspace,
):
    template = PLAN_PROMPT + "\nAlso: keep the plan to five lines.\n"
    messages = _sent(workspace, PLAN, template=template)
    assert "PLAN stage" not in messages[0]["content"]
    assert messages[1]["content"].startswith("You are the PLAN stage.")
    assert "keep the plan to five lines" in messages[1]["content"]
