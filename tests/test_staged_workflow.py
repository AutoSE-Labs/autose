"""DR-003/005/008/009: the craft pipeline end to end, with fakes."""

from __future__ import annotations

import itertools

import pytest

from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.errors import IterationBudgetExhausted, TimeBudgetExhausted
from logic.inference.types import CompletionResult, Usage
from logic.limits import UNLIMITED
from logic.tiers import CRAFT
from logic.workflows import WorkflowRequest
from logic.workflows.craft import CraftWorkflow
from logic.workflows.staged import DEFAULT_BOUNDS, StagedWorkflow, WorkflowBounds
from logic.workflows.stages import EXECUTE, PLAN, STAGES, VALIDATE
from tests.support.fakes import RecordingApprovalGate, ScriptedChatModel

# -- artifact builders --------------------------------------------------------


def plan(*ids: str) -> str:
    lines = [f"- {rid} [Not Started] — Requirement {rid}" for rid in ids]
    return "The plan.\n\n## REQUIREMENTS STATUS\n" + "\n".join(lines)


def execute(*ids: str, status: str = "Implemented") -> str:
    lines = [
        f"- {rid} [{status}] — Requirement {rid} — Evidence: src/{rid}.py"
        for rid in ids
    ]
    return "Did the work.\n\n## REQUIREMENTS STATUS\n" + "\n".join(lines)


def validate(*ids: str, status: str = "Verified", verdict: str = "VERDICT: PASS") -> str:
    lines = [
        f"- {rid} [{status}] — Requirement {rid} — Evidence: read src/{rid}.py"
        for rid in ids
    ]
    return "Checked it.\n\n## REQUIREMENTS STATUS\n" + "\n".join(lines) + f"\n\n{verdict}"


def _answer(text: str, tokens: int = 5) -> CompletionResult:
    return CompletionResult(
        content=text, finish_reason="stop", usage=Usage(prompt_tokens=tokens, completion_tokens=1)
    )


def _request(tmp_path, script, *, events=None, gate=None) -> WorkflowRequest:
    return WorkflowRequest(
        prompt="add a retry helper",
        workspace=Workspace.at(tmp_path),
        model=ScriptedChatModel([_answer(item) for item in script]),
        events=events or RecordingEventSink(),
        approval_gate=gate or RecordingApprovalGate(default=True),
    )


# -- the happy path -------------------------------------------------------------


def test_craft_runs_plan_execute_validate_and_completes(tmp_path):
    request = _request(
        tmp_path, [plan("R1", "R2"), execute("R1", "R2"), validate("R1", "R2")]
    )
    outcome = CraftWorkflow().run(request)
    assert outcome.succeeded
    assert outcome.stop_reason == "completed"
    assert set(outcome.artifacts) == {"plan", "execution_summary", "validation_report"}


def test_usage_is_accumulated_across_every_stage(tmp_path):
    request = _request(tmp_path, [plan("R1"), execute("R1"), validate("R1")])
    outcome = CraftWorkflow().run(request)
    assert outcome.usage.total_tokens == 3 * (5 + 1)


def test_the_ledger_is_stripped_from_the_stored_artifact(tmp_path):
    """DR-004: RequestMemory owns the ledger; a second textual copy could drift."""
    request = _request(tmp_path, [plan("R1"), execute("R1"), validate("R1")])
    outcome = CraftWorkflow().run(request)
    assert "REQUIREMENTS STATUS" not in outcome.artifacts["plan"]
    assert "The plan." in outcome.artifacts["plan"]


def test_each_stage_receives_the_prior_stages_artifacts(tmp_path):
    model = ScriptedChatModel(
        [_answer(t) for t in (plan("R1"), execute("R1"), validate("R1"))]
    )
    CraftWorkflow().run(
        WorkflowRequest(
            prompt="p",
            workspace=Workspace.at(tmp_path),
            model=model,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
        )
    )
    execute_prompt = model.requests[1]["messages"][-1]["content"]
    assert "Implementation plan" in execute_prompt
    assert "Requirements ledger" in execute_prompt


def test_craft_never_mentions_a_design_document_it_has_no_stage_for(tmp_path):
    model = ScriptedChatModel(
        [_answer(t) for t in (plan("R1"), execute("R1"), validate("R1"))]
    )
    CraftWorkflow().run(
        WorkflowRequest(
            prompt="p",
            workspace=Workspace.at(tmp_path),
            model=model,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
        )
    )
    assert "Design decision" not in model.requests[1]["messages"][-1]["content"]


# -- DR-005: the ledger is a contract -------------------------------------------


def test_a_claimed_pass_with_an_unverified_requirement_is_overridden(tmp_path):
    """The core DR-005 property: 'passed' is not accepted at face value."""
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1", "R2"),
            execute("R1", "R2"),
            # Says PASS while R2 is Failed.
            "Checked.\n\n## REQUIREMENTS STATUS\n"
            "- R1 [Verified] — Requirement R1 — Evidence: x\n"
            "- R2 [Failed] — Requirement R2 — Evidence: broken\n\nVERDICT: PASS",
            execute("R1", "R2"),
            validate("R1", "R2"),
        ],
        events=events,
    )
    outcome = CraftWorkflow().run(request)
    assert outcome.succeeded

    verdicts = events.of_type("validation_verdict")
    assert verdicts[0].data["overridden"] is True
    assert verdicts[0].data["unmet"] == ["R2"]
    assert "R2" in verdicts[0].data["feedback"]


def test_a_round_that_only_left_requirements_unchecked_is_re_validated(tmp_path):
    """Nothing was found broken, so execute is not asked to fix anything.

    A stalled validator once sent five unchecked requirements back to execute
    with the feedback "re-run these checks" — work only validation can do.
    """
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1", "R2"),
            execute("R1", "R2"),
            "Ran out.\n\n## REQUIREMENTS STATUS\n"
            "- R1 [Verified] — Requirement R1 — Evidence: ran it\n"
            "- R2 [Unverified] — Requirement R2 — Evidence: no check ran\n\n"
            "VERDICT: FAIL execute — R2 was not checked",
            validate("R1", "R2"),
        ],
        events=events,
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    loopbacks = events.of_type("loopback")
    assert [event.data["target"] for event in loopbacks] == [VALIDATE]
    assert "R2" in loopbacks[0].data["feedback"]
    starts = [event.data["stage"] for event in events.of_type("stage_started")]
    assert starts == [PLAN, EXECUTE, VALIDATE, VALIDATE]


def test_a_second_unchecked_round_in_a_row_goes_to_execute(tmp_path):
    """Re-checking is offered once; a requirement that still cannot be checked
    may need the implementation to change."""
    unchecked = (
        "Ran out.\n\n## REQUIREMENTS STATUS\n"
        "- R1 [Unverified] — Requirement R1 — Evidence: no check ran\n\n"
        "VERDICT: FAIL execute — R1 was not checked"
    )
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [plan("R1"), execute("R1"), unchecked, unchecked, execute("R1"), validate("R1")],
        events=events,
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    assert [event.data["target"] for event in events.of_type("loopback")] == [
        VALIDATE,
        EXECUTE,
    ]


def test_an_observed_failure_still_goes_to_execute(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1", "R2"),
            execute("R1", "R2"),
            "Checked.\n\n## REQUIREMENTS STATUS\n"
            "- R1 [Failed] — Requirement R1 — Evidence: crashes\n"
            "- R2 [Unverified] — Requirement R2 — Evidence: no check ran\n\n"
            "VERDICT: FAIL execute — R1 crashes",
            execute("R1", "R2"),
            validate("R1", "R2"),
        ],
        events=events,
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    assert [event.data["target"] for event in events.of_type("loopback")] == [EXECUTE]


def test_every_requirement_verified_but_no_verdict_line_fails_closed(tmp_path):
    """An unreadable gate is not a passed gate."""
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1"),
            execute("R1"),
            validate("R1", verdict="I think it is fine"),
            execute("R1"),
            validate("R1"),
        ],
        events=events,
    )
    outcome = CraftWorkflow().run(request)
    assert outcome.succeeded
    assert "VERDICT: PASS" in events.of_type("validation_verdict")[0].data["feedback"]


def test_a_validator_may_fail_a_round_with_every_requirement_verified(tmp_path):
    """The ledger is necessary but not sufficient (DR-005)."""
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1"),
            execute("R1"),
            validate("R1", verdict="VERDICT: FAIL execute — it breaks existing callers"),
            execute("R1"),
            validate("R1"),
        ],
        events=events,
    )
    outcome = CraftWorkflow().run(request)
    assert outcome.succeeded
    first = events.of_type("validation_verdict")[0]
    assert first.data["passed"] is False
    assert first.data["overridden"] is False
    assert "breaks existing callers" in first.data["feedback"]


def test_a_stage_that_invents_a_requirement_is_rejected_and_repaired(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1"),
            execute("R1", "R9"),  # invented R9
            execute("R1"),
            validate("R1"),
        ],
        events=events,
    )
    outcome = CraftWorkflow().run(request)
    assert outcome.succeeded
    retries = events.of_type("stage_repaired")
    assert retries and "invented R9" in retries[0].data["reason"]


def test_a_stage_that_drops_a_requirement_is_rejected_and_repaired(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [plan("R1", "R2"), execute("R1"), execute("R1", "R2"), validate("R1", "R2")],
        events=events,
    )
    assert CraftWorkflow().run(request).succeeded
    assert "dropped R2" in events.of_type("stage_repaired")[0].data["reason"]


def test_a_malformed_ledger_is_repaired_with_the_parser_complaint(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        ["a plan with no ledger", plan("R1"), execute("R1"), validate("R1")],
        events=events,
    )
    assert CraftWorkflow().run(request).succeeded
    assert "REQUIREMENTS STATUS" in events.of_type("stage_repaired")[0].data["reason"]


def test_a_stage_that_cannot_produce_a_valid_output_fails_the_workflow(tmp_path):
    """Giving up on a stage needs a configured retry cap now (DR-030).

    Without one the runner keeps handing the parser's complaint back for as
    long as the user lets it, which is what `test_format_retries_are_unlimited`
    covers in tests/test_limits.py.
    """
    # Each attempt is followed by one in-place repair, so twice the replies.
    request = _request(tmp_path, ["no ledger"] * 6)
    workflow = StagedWorkflow(
        CRAFT, (PLAN, EXECUTE, VALIDATE), bounds=WorkflowBounds(max_format_retries=2)
    )
    outcome = workflow.run(request)
    assert not outcome.succeeded
    assert outcome.stop_reason == "stage_failed"
    assert "plan stage failed" in outcome.answer
    assert "after 3 attempt(s)" in outcome.answer


def test_a_rejected_stage_output_never_becomes_canonical_state(tmp_path):
    request = _request(
        tmp_path,
        ["bad plan text with no ledger", plan("R1"), execute("R1"), validate("R1")],
    )
    outcome = CraftWorkflow().run(request)
    assert "bad plan text" not in outcome.artifacts["plan"]


# -- DR-009: bounds -------------------------------------------------------------


def test_the_loopback_cap_stops_the_workflow_and_is_not_a_success(tmp_path):
    failing = validate("R1", status="Failed", verdict="VERDICT: FAIL execute — nope")
    request = _request(
        tmp_path,
        [plan("R1"), execute("R1"), failing, execute("R1"), failing, execute("R1"), failing],
    )
    workflow = StagedWorkflow(
        CRAFT, (PLAN, EXECUTE, VALIDATE), bounds=WorkflowBounds(max_validation_iterations=3)
    )
    outcome = workflow.run(request)
    assert not outcome.succeeded
    assert outcome.stop_reason == "attempts_exhausted"
    # A cap of 3 means three validation rounds: two send-backs, then a stop.
    assert "3 time(s)" in outcome.answer
    assert "NOT complete" in outcome.answer


def test_the_time_budget_stops_the_workflow_and_is_not_a_success(tmp_path):
    ticks = itertools.count(0, 1000)  # every check advances 1000 seconds
    request = _request(tmp_path, [plan("R1"), execute("R1"), validate("R1")])
    workflow = StagedWorkflow(
        CRAFT,
        (PLAN, EXECUTE, VALIDATE),
        bounds=WorkflowBounds(time_budget_seconds=1.0),
        clock=lambda: next(ticks),
    )
    outcome = workflow.run(request)
    assert not outcome.succeeded
    assert outcome.stop_reason == "time_budget_exhausted"
    assert "time budget" in outcome.answer


def test_the_two_bounds_are_reported_distinctly(tmp_path):
    assert IterationBudgetExhausted("x").__class__ is not TimeBudgetExhausted
    assert issubclass(IterationBudgetExhausted, Exception)


def test_the_bounds_are_announced_when_the_workflow_starts(tmp_path):
    """DR-030: an Adapter is told there is no bound, not shown the -1 sentinel."""
    events = RecordingEventSink()
    request = _request(
        tmp_path, [plan("R1"), execute("R1"), validate("R1")], events=events
    )
    CraftWorkflow().run(request)
    started = events.of_type("workflow_started")[0].data
    assert started["max_validation_iterations"] is None
    assert started["time_budget_seconds"] is None
    assert started["unlimited"] is True


def test_configured_bounds_are_announced_as_numbers(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path, [plan("R1"), execute("R1"), validate("R1")], events=events
    )
    StagedWorkflow(
        CRAFT,
        (PLAN, EXECUTE, VALIDATE),
        bounds=WorkflowBounds(max_validation_iterations=3, time_budget_seconds=60.0),
    ).run(request)
    started = events.of_type("workflow_started")[0].data
    assert started["max_validation_iterations"] == 3
    assert started["time_budget_seconds"] == 60.0
    assert started["unlimited"] is False


def test_every_tier_is_unbounded_by_default_and_bounded_per_tier(tmp_path):
    """DR-030: the per-tier sections stay, the per-tier default numbers go."""
    from logic.tuning import TierBounds, Tuning

    assert DEFAULT_BOUNDS.unlimited
    for tier in ("spark", "craft"):
        assert getattr(Tuning(), tier) == TierBounds()

    # And a repo can still bound one tier without touching the other.
    tuning = Tuning.from_mapping({"craft": {"max_tool_rounds": 7}})
    assert tuning.craft.max_tool_rounds == 7
    assert tuning.spark.max_tool_rounds == UNLIMITED


def test_a_workflow_without_a_validation_stage_is_refused():
    with pytest.raises(ValueError, match="no validation stage"):
        StagedWorkflow("craft", (PLAN, EXECUTE))


# -- loopback routing -------------------------------------------------------------


def test_loopback_returns_to_the_named_stage(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1"),
            execute("R1"),
            validate("R1", verdict="VERDICT: FAIL plan — the plan is wrong"),
            plan("R1"),
            execute("R1"),
            validate("R1"),
        ],
        events=events,
    )
    assert CraftWorkflow().run(request).succeeded
    assert events.of_type("loopback")[0].data["target"] == PLAN


def test_craft_routes_a_design_loopback_to_a_stage_it_actually_has(tmp_path):
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [
            plan("R1"),
            execute("R1"),
            validate("R1", verdict="VERDICT: FAIL design — architecture is wrong"),
            execute("R1"),
            validate("R1"),
        ],
        events=events,
    )
    assert CraftWorkflow().run(request).succeeded
    assert events.of_type("loopback")[0].data["target"] == EXECUTE


def test_a_retried_stage_is_told_what_it_already_tried(tmp_path):
    model = ScriptedChatModel(
        [
            _answer(t)
            for t in (
                plan("R1"),
                execute("R1"),
                validate("R1", verdict="VERDICT: FAIL execute — missing the error path"),
                execute("R1"),
                validate("R1"),
            )
        ]
    )
    CraftWorkflow().run(
        WorkflowRequest(
            prompt="p",
            workspace=Workspace.at(tmp_path),
            model=model,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
        )
    )
    retried_prompt = model.requests[3]["messages"][-1]["content"]
    assert "Why this was sent back" in retried_prompt
    assert "missing the error path" in retried_prompt
    assert "Earlier attempts" in retried_prompt


# -- DR-008: stage permissions ------------------------------------------------------


def test_the_validation_stage_has_no_edit_tool_in_its_schema(tmp_path):
    """Validate may write scratch scripts (confined, see
    test_the_validation_stage_writes_only_scratch_scripts) but never edit."""
    model = ScriptedChatModel(
        [_answer(t) for t in (plan("R1"), execute("R1"), validate("R1"))]
    )
    CraftWorkflow().run(
        WorkflowRequest(
            prompt="p",
            workspace=Workspace.at(tmp_path),
            model=model,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
        )
    )
    validate_tools = {
        entry["function"]["name"] for entry in model.requests[2]["tools"]
    }
    assert "edit_file" not in validate_tools
    assert "read_file" in validate_tools


def test_the_planning_stage_is_read_only(tmp_path):
    assert not STAGES[PLAN].can_write


def test_files_changed_are_tracked_through_the_write_tools(tmp_path):
    """The runner instruments its own cloned registry (DR-008)."""
    target = tmp_path / "thing.py"
    target.write_text("old\n", encoding="utf-8")

    from logic.inference.types import ToolCall

    editing = ScriptedChatModel(
        [
            _answer(plan("R1")),
            # The execute stage edits the file, then reports.
            CompletionResult(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_file",
                        arguments={
                            "path": "thing.py",
                            "edits": [{"old_str": "old", "new_str": "new"}],
                        },
                        raw_arguments="{}",
                    ),
                )
            ),
            _answer(execute("R1")),
            _answer(validate("R1")),
        ]
    )
    outcome = CraftWorkflow().run(
        WorkflowRequest(
            prompt="p",
            workspace=Workspace.at(tmp_path),
            model=editing,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
        )
    )
    assert target.read_text() == "new\n"
    assert outcome.files_changed == ("thing.py",)


def test_changed_files_are_structured_data_not_answer_prose(tmp_path):
    """Rendered once, by the Adapter, from the outcome field — and kept out of
    session history, which would otherwise replay a file listing every turn."""
    target = tmp_path / "thing.py"
    target.write_text("old\n", encoding="utf-8")
    from logic.inference.types import ToolCall

    model = ScriptedChatModel(
        [
            _answer(plan("R1")),
            CompletionResult(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_file",
                        arguments={
                            "path": "thing.py",
                            "edits": [{"old_str": "old", "new_str": "new"}],
                        },
                        raw_arguments="{}",
                    ),
                )
            ),
            _answer(execute("R1")),
            _answer(validate("R1")),
        ]
    )
    outcome = CraftWorkflow().run(
        WorkflowRequest(
            prompt="p",
            workspace=Workspace.at(tmp_path),
            model=model,
            events=RecordingEventSink(),
            approval_gate=RecordingApprovalGate(default=True),
        )
    )
    assert outcome.files_changed == ("thing.py",)
    assert "Files changed" not in outcome.answer
    assert "thing.py" not in outcome.answer


def test_validate_scratch_scripts_are_removed_and_not_reported(tmp_path):
    from logic.inference.types import ToolCall
    from logic.workflows.stages import VALIDATE_SCRATCH_DIRECTORY

    script_call = CompletionResult(
        content="",
        finish_reason="tool_calls",
        tool_calls=(
            ToolCall(
                id="w",
                name="write_file",
                arguments={
                    "path": f"{VALIDATE_SCRATCH_DIRECTORY}/check.py",
                    "content": "print('ok')\n",
                },
            ),
        ),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
    )
    request = WorkflowRequest(
        prompt="add a retry helper",
        workspace=Workspace.at(tmp_path),
        model=ScriptedChatModel(
            [
                _answer(plan("R1")),
                _answer(execute("R1")),
                script_call,
                _answer(validate("R1")),
            ]
        ),
        events=RecordingEventSink(),
        approval_gate=RecordingApprovalGate(default=True),
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    assert not (tmp_path / VALIDATE_SCRATCH_DIRECTORY).exists()
    assert not any(VALIDATE_SCRATCH_DIRECTORY in str(f) for f in outcome.files_changed)


def test_a_later_validation_pass_reruns_earlier_checks(tmp_path, monkeypatch):
    from logic.inference.types import ToolCall
    from logic.workflows import stages
    from logic.workflows.staged import EARLIER_CHECKS_NOTICE
    from logic.workflows.stages import VALIDATE_SCRATCH_DIRECTORY

    monkeypatch.setattr(stages, "CONFIRMING_VALIDATION_PASSES", 1)
    script_call = CompletionResult(
        content="",
        finish_reason="tool_calls",
        tool_calls=(
            ToolCall(
                id="w",
                name="write_file",
                arguments={
                    "path": f"{VALIDATE_SCRATCH_DIRECTORY}/check_rows.py",
                    "content": "print('two spans in one row')\n",
                },
            ),
        ),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
    )
    seen: list[bool] = []
    model = ScriptedChatModel(
        [
            _answer(plan("R1")),
            _answer(execute("R1")),
            script_call,
            _answer(validate("R1")),
            _answer(validate("R1")),
        ]
    )
    original = model._next

    def recording_next(*args, **kwargs):
        seen.append(
            (tmp_path / VALIDATE_SCRATCH_DIRECTORY / "earlier_check_rows.py").exists()
        )
        return original(*args, **kwargs)

    model._next = recording_next
    request = WorkflowRequest(
        prompt="add a retry helper",
        workspace=Workspace.at(tmp_path),
        model=model,
        events=RecordingEventSink(),
        approval_gate=RecordingApprovalGate(default=True),
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    # Absent while the first pass ran, back for the confirming pass.
    assert seen == [False, False, False, False, True]
    last_prompt = str(model.requests[-1]["messages"])
    assert EARLIER_CHECKS_NOTICE.split(".")[0] in last_prompt
    assert not (tmp_path / VALIDATE_SCRATCH_DIRECTORY).exists()


def test_a_pass_must_be_confirmed_by_an_independent_validation(tmp_path, monkeypatch):
    from logic.workflows import stages

    monkeypatch.setattr(stages, "CONFIRMING_VALIDATION_PASSES", 1)
    events = RecordingEventSink()
    request = _request(
        tmp_path,
        [plan("R1"), execute("R1"), validate("R1"), validate("R1")],
        events=events,
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    starts = [event.data["stage"] for event in events.of_type("stage_started")]
    assert starts == [PLAN, EXECUTE, VALIDATE, VALIDATE]
    assert len(events.of_type("validation_confirmation")) == 1


def test_a_confirming_validation_that_fails_sends_work_back(tmp_path, monkeypatch):
    from logic.workflows import stages

    monkeypatch.setattr(stages, "CONFIRMING_VALIDATION_PASSES", 1)
    events = RecordingEventSink()
    failed = validate("R1", status="Failed", verdict="VERDICT: FAIL execute — misses a rule")
    request = _request(
        tmp_path,
        [plan("R1"), execute("R1"), validate("R1"), failed, execute("R1"), validate("R1")],
        events=events,
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.succeeded
    assert [event.data["target"] for event in events.of_type("loopback")] == [EXECUTE]
    # Confirmed once per run, not again after the loopback.
    assert len(events.of_type("validation_confirmation")) == 1
