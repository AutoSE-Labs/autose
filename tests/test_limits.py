"""DR-030: every cap is unlimited by default, and the user is what stops a run.

Two halves, and they only make sense together: removing the caps is safe *only*
because a person can stop the work, so the same file covers "it no longer stops
on its own" and "asking it to stop works, and keeps what was done".
"""

from __future__ import annotations

import itertools

import pytest

from logic.common.agent import (
    _MAX_IDENTICAL_READ_ONLY_CALLS,
    DEFAULT_MAX_TOOL_ROUNDS,
    BaseAgent,
)
from logic.common.cancel import CancelToken
from logic.common.context import ContextBudget
from logic.common.events import RecordingEventSink
from logic.common.tool_output import BoundedToolOutput
from logic.common.tools import catalog_subset
from logic.common.tools.editing import edit_file
from logic.common.workspace import Workspace
from logic.errors import RunCancelled
from logic.limits import (
    UNLIMITED,
    count_limit,
    counted,
    describe_limit,
    duration_limit,
    is_unlimited,
)
from logic.tiers import CRAFT
from logic.tuning import TierBounds, Tuning
from logic.workflows.base import WorkflowRequest
from logic.workflows.craft import CraftWorkflow
from logic.workflows.staged import DEFAULT_BOUNDS
from tests.support.fakes import (
    RecordingApprovalGate,
    ScriptedChatModel,
    answer,
    calls,
    tool_call,
)
from tests.test_staged_workflow import _answer, execute, plan, validate

# -- the sentinel ---------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, -2, -100.0, None])
def test_any_negative_value_and_none_mean_unlimited(value):
    """-1 is the documented convention, not a magic number to match exactly."""
    assert is_unlimited(value)
    assert count_limit(value) is None
    assert duration_limit(value) is None


def test_a_count_of_zero_is_raised_to_the_minimum_but_a_duration_is_unlimited():
    """A stage allowed no attempts cannot answer; a run of no seconds cannot start."""
    assert count_limit(0) == 1
    assert count_limit(0, minimum=0) == 0
    assert duration_limit(0) is None


def test_counted_stops_at_a_limit_and_never_when_unlimited():
    assert list(counted(3)) == [1, 2, 3]
    assert list(itertools.islice(counted(None), 5)) == [1, 2, 3, 4, 5]


def test_describe_limit_says_unlimited_rather_than_printing_the_sentinel():
    assert describe_limit(UNLIMITED) == "unlimited"
    assert describe_limit(None, "seconds") == "unlimited"
    assert describe_limit(30) == "30"
    assert describe_limit(3600.0, "seconds") == "3600 seconds"


# -- the defaults ---------------------------------------------------------------


def test_every_work_stopping_cap_is_unlimited_by_default():
    assert DEFAULT_MAX_TOOL_ROUNDS == UNLIMITED
    assert TierBounds() == TierBounds(
        max_tool_rounds=UNLIMITED,
        max_validation_iterations=UNLIMITED,
        time_budget_seconds=UNLIMITED,
        max_format_retries=UNLIMITED,
    )
    assert DEFAULT_BOUNDS.unlimited
    for tier in ("spark", "craft"):
        assert getattr(Tuning(), tier) == TierBounds()


def test_the_caps_that_shape_one_request_keep_real_defaults():
    """DR-030 removed the caps that stop work, not the ones that fit a window."""
    tuning = Tuning()
    assert tuning.context.max_history_rounds > 0
    assert tuning.tool_output.max_chars > 0
    assert tuning.shell.command_timeout_seconds > 0
    assert tuning.session.max_history_exchanges > 0


def test_a_config_file_turns_any_cap_off_with_minus_one():
    tuning = Tuning.from_mapping(
        {
            "context": {"max_history_rounds": -1},
            "tool_output": {"max_chars": -1, "max_artifacts": -1},
            "shell": {"command_timeout_seconds": -1},
            "editing": {"max_edits_per_call": -1},
            "classifier": {"max_output_tokens": -1},
            "spark": {"max_tool_rounds": 12},
        }
    )
    assert tuning.context.max_history_rounds == UNLIMITED
    assert tuning.tool_output.max_chars == UNLIMITED
    assert tuning.editing.max_edits_per_call == UNLIMITED
    # And a repo that wants a bound back gets exactly the one it asked for.
    assert tuning.spark.max_tool_rounds == 12
    assert tuning.craft.max_tool_rounds == UNLIMITED


# -- unlimited rounds -----------------------------------------------------------


class _Stage(BaseAgent):
    name = "test-stage"

    def system_prompt(self) -> str:
        return "You are a test stage."


def _looping_agent(workspace, rounds: int, **kwargs) -> tuple[_Stage, RecordingEventSink]:
    """A stage that works for *rounds* rounds and then answers.

    Each round reads a *different* file, because that is what genuine work in
    many small steps looks like — which is the case DR-030 lifted the round cap
    for. A script that re-read one file every round would instead be modelling
    a stall, and is stopped by the no-progress guard rather than running to the
    end (see `test_repeated_identical_reads_are_stopped_as_a_stall`).
    """
    for index in range(rounds):
        (workspace.root / f"m{index}.txt").write_text(f"x{index}", encoding="utf-8")
    script = [
        calls(tool_call("read_file", path=f"m{index}.txt"), content="still looking")
        for index in range(rounds)
    ] + [answer("finished")]
    events = RecordingEventSink()
    agent = _Stage(
        model=ScriptedChatModel(script),
        tools=catalog_subset("read_file"),
        workspace=workspace,
        events=events,
        **kwargs,
    )
    return agent, events


def test_a_tool_loop_past_the_old_cap_of_thirty_now_finishes(workspace):
    agent, events = _looping_agent(workspace, 45)
    result = agent.run("keep reading")
    assert result.stop_reason == "answered"
    assert result.content == "finished"
    assert result.rounds == 46
    assert not events.of_type("agent_round_cap_reached")


def test_a_configured_round_cap_still_stops_the_loop(workspace):
    agent, events = _looping_agent(workspace, 45, max_tool_rounds=4)
    result = agent.run("keep reading")
    assert result.stop_reason == "round_cap"
    assert events.of_type("agent_round_cap_reached")


def test_format_retries_are_unlimited(tmp_path):
    """The old cap of 2 retries gave up on the third malformed ledger."""
    script = [_answer("no ledger here")] * 5 + [
        _answer(plan("R1")),
        _answer(execute("R1")),
        _answer(validate("R1")),
    ]
    outcome = CraftWorkflow().run(_craft_request(tmp_path, script))
    assert outcome.succeeded, outcome.answer


def test_validation_may_send_work_back_indefinitely(tmp_path):
    failing = validate("R1", status="Failed", verdict="VERDICT: FAIL execute — nope")
    script = [plan("R1")] + [execute("R1"), failing] * 5 + [
        execute("R1"),
        validate("R1"),
    ]
    outcome = CraftWorkflow().run(
        _craft_request(tmp_path, [_answer(item) for item in script])
    )
    assert outcome.succeeded, outcome.answer


def test_an_untimed_run_still_reports_how_long_it_took(tmp_path):
    """No budget must not mean no clock: the elapsed figure is what a user reads."""
    ticks = itertools.count(0, 1000)
    events = RecordingEventSink()
    request = _craft_request(
        tmp_path,
        [_answer(plan("R1")), _answer(execute("R1")), _answer(validate("R1"))],
        events=events,
    )
    from logic.workflows.staged import StagedWorkflow
    from logic.workflows.stages import EXECUTE, PLAN, VALIDATE

    outcome = StagedWorkflow(
        CRAFT, (PLAN, EXECUTE, VALIDATE), clock=lambda: next(ticks)
    ).run(request)
    assert outcome.succeeded
    assert events.of_type("workflow_finished")[0].data["elapsed_seconds"] > 0


# -- stopping ------------------------------------------------------------------


def test_a_token_reports_who_asked_first_and_can_be_re_armed():
    token = CancelToken()
    assert not token.requested
    assert token.request("ctrl-c") is True
    assert token.request("again") is False
    assert token.reason == "ctrl-c"  # the first reason wins, not the last
    token.clear()
    assert not token.requested
    assert token.reason == ""


def test_stopping_an_agent_keeps_the_draft_it_had_written(workspace):
    token = CancelToken()
    agent, events = _looping_agent(workspace, 45, cancel=token)
    token.request("you pressed ctrl-c")

    result = agent.run("keep reading")

    assert result.stop_reason == "cancelled"
    assert not result.complete
    assert events.of_type("agent_cancelled")


def test_a_stop_mid_round_skips_the_tool_calls_that_had_not_run_yet(workspace):
    """A stop lands between tool calls, never in the middle of one."""
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    token = CancelToken()

    class _StopOnFirstToolResult(RecordingEventSink):
        def emit(self, event):
            super().emit(event)
            if event.type == "tool_completed":
                token.request("stop now")

    events = _StopOnFirstToolResult()
    model = ScriptedChatModel(
        [
            calls(
                tool_call("read_file", path="m.txt"),
                tool_call("read_file", path="m.txt"),
                content="looking",
            ),
            answer("never reached"),
        ]
    )
    agent = _Stage(
        model=model,
        tools=catalog_subset("read_file"),
        workspace=workspace,
        events=events,
        cancel=token,
    )

    result = agent.run("read both")

    assert result.stop_reason == "cancelled"
    # The first call ran; the second was not dispatched, so the stop cost one
    # tool call rather than the whole round.
    assert len(events.of_type("tool_completed")) == 1
    # The skipped call is answered in the transcript but not counted as work.
    assert result.tool_calls == 1


def test_a_stop_mid_stream_abandons_the_round_still_generating(workspace):
    """A stop must not wait for the round it lands in to finish streaming.

    The first delta of the round in progress has already reached the caller
    when the stop is asked for — that text stays on screen — but the round
    itself is abandoned rather than completed: the result falls back to what
    the previous, finished round had written (DR-030).
    """
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    token = CancelToken()

    class _StopAfterFirstDelta(ScriptedChatModel):
        """Requests a stop after the second round's first streamed chunk."""

        def stream(self, *args, **kwargs):
            chunks = super().stream(*args, **kwargs)
            first = next(chunks)
            yield first
            if self.call_count == 2:
                token.request("stop now")
            yield from chunks

    model = _StopAfterFirstDelta(
        [
            calls(tool_call("read_file", path="m.txt"), content="draft answer"),
            answer("a much longer second reply that never finishes streaming"),
        ]
    )
    events = RecordingEventSink()
    agent = _Stage(
        model=model,
        tools=catalog_subset("read_file"),
        workspace=workspace,
        events=events,
        cancel=token,
    )

    stream = agent.stream("read then answer")
    streamed = "".join(stream)
    result = stream.result

    # The finished first round streamed in full, and the abandoned second
    # round's first delta reached the caller before the stop took effect...
    assert streamed == "draft answer" + "a much l"
    # ...but the second round never completed, so the result is the first
    # round's content, not a sentence cut off mid-word.
    assert result.stop_reason == "cancelled"
    assert result.content == "draft answer"
    assert events.of_type("agent_cancelled")
    # The model was never asked for anything past the abandoned round.
    assert model.call_count == 2


def test_stopping_a_staged_run_keeps_its_finished_artifacts(tmp_path):
    token = CancelToken()
    script = [_answer(plan("R1"))] + [_answer(execute("R1"))] * 4

    class _StopAfterPlan(ScriptedChatModel):
        """Presses ctrl-c the moment the first stage finishes streaming."""

        def stream(self, *args, **kwargs):
            yield from super().stream(*args, **kwargs)
            token.request("you pressed ctrl-c")

    request = WorkflowRequest(
        prompt="add a helper",
        workspace=Workspace.at(tmp_path),
        model=_StopAfterPlan(script),
        events=RecordingEventSink(),
        approval_gate=RecordingApprovalGate(default=True),
        cancel=token,
    )
    outcome = CraftWorkflow().run(request)

    assert outcome.stop_reason == "cancelled"
    assert not outcome.succeeded
    # The plan the first stage produced is not thrown away with the run.
    assert "plan" in outcome.artifacts
    assert "Stopped at your request" in outcome.answer
    assert "you pressed ctrl-c" in outcome.answer


def test_a_stopped_run_is_never_reported_as_a_success(tmp_path):
    """DR-009's rule, extended to DR-030's new stop reason."""
    from logic.workflows.base import WorkflowOutcome

    assert not WorkflowOutcome(tier=CRAFT, answer="", stop_reason="cancelled").succeeded


def test_the_error_carries_the_stage_it_stopped_in():
    error = RunCancelled("stopped", stage="execute", reason="ctrl-c")
    assert error.stage == "execute"
    assert error.reason == "ctrl-c"


# -- the shaping caps still work when turned off -------------------------------


def test_unbounded_tool_output_passes_the_whole_result_through():
    bounded = BoundedToolOutput(max_chars=UNLIMITED)
    result = "x" * 50_000
    content, location = bounded.apply("read_file", {}, result)
    assert content == result
    assert location is None


def test_an_unbounded_edit_batch_is_accepted(tmp_path):
    workspace = Workspace.at(tmp_path)
    target = tmp_path / "many.txt"
    # Zero-padded so no `old_str` is a prefix of another line: DR-007 refuses an
    # ambiguous match, which has nothing to do with how many edits are allowed.
    target.write_text("\n".join(f"line {n:03d}" for n in range(40)), encoding="utf-8")
    edits = [
        {"old_str": f"line {n:03d}", "new_str": f"row {n:03d}"} for n in range(40)
    ]

    refused = edit_file("many.txt", edits, workspace=workspace, max_edits_per_call=20)
    assert refused.startswith("Error:")

    accepted = edit_file(
        "many.txt", edits, workspace=workspace, max_edits_per_call=UNLIMITED
    )
    assert accepted.startswith("Edited 40 block(s)")


def test_unbounded_history_keeps_every_round():
    budget = ContextBudget(max_history_rounds=UNLIMITED)
    messages = [{"role": "system", "content": "s"}] + [
        {"role": "assistant", "content": f"round {n}"} for n in range(50)
    ]
    prepared = budget.prepare(messages)
    assert len(prepared.messages) == len(messages)


# -- helpers -------------------------------------------------------------------


def _craft_request(tmp_path, script, *, events=None, cancel=None) -> WorkflowRequest:
    return WorkflowRequest(
        prompt="add a retry helper",
        workspace=Workspace.at(tmp_path),
        model=ScriptedChatModel(script),
        events=events or RecordingEventSink(),
        approval_gate=RecordingApprovalGate(default=True),
        cancel=cancel or CancelToken(),
    )


# -- the no-progress guard ------------------------------------------------------
#
# DR-030 made rounds unlimited, which removed the only thing that ended a call
# the model could not finish. What ends one now is the absence of progress, so
# these cover the shapes a stall actually takes in a trace: an identical call
# repeated, and the same call respelled to dodge the repeat tally.


def _stalling_agent(workspace, script, **kwargs) -> tuple[_Stage, RecordingEventSink]:
    (workspace.root / "m.txt").write_text("x", encoding="utf-8")
    events = RecordingEventSink()
    agent = _Stage(
        model=ScriptedChatModel(script),
        tools=catalog_subset("read_file", "list_files"),
        workspace=workspace,
        events=events,
        **kwargs,
    )
    return agent, events


def test_repeated_identical_reads_are_stopped_as_a_stall(workspace):
    """Unlimited rounds must not mean an unlimited loop.

    Nothing in the script ever answers, so before the no-progress guard this
    ran until the user pressed Stop.
    """
    script = [
        calls(tool_call("read_file", path="m.txt"), content="still looking")
        for _ in range(60)
    ]
    agent, events = _stalling_agent(workspace, script)
    result = agent.run("explain this project")

    assert result.stop_reason == "stalled"
    # Stopped in the first handful of rounds rather than running the script out.
    assert result.rounds < 10
    stalled = events.of_type("agent_stalled")
    assert stalled
    assert "learned nothing new" in stalled[0].message


def test_respelling_a_path_does_not_dodge_the_repeat_tally(workspace):
    """`.`, `./` and `.//.` are one directory, so they are one call.

    The tally keys on arguments, and a model that is stalling varies them
    without varying the answer — which is how a loop survived a guard that
    compared them as typed.
    """
    spellings = [".", "./", ".//.", "  .  ", "./././"]
    script = [
        calls(tool_call("list_files", directory=spelling), content="looking")
        for spelling in spellings
    ] + [answer("finished")]
    agent, events = _stalling_agent(workspace, script)
    agent.run("look around")

    refusals = events.of_type("tool_call_repeated")
    assert refusals, "every spelling was treated as a fresh call"
    # The tally keeps climbing across *different* spellings, which is the proof
    # they share one key: compared as typed, each would have sat at count 1.
    assert [event.data["count"] for event in refusals] == [
        _MAX_IDENTICAL_READ_ONLY_CALLS + 1,
        _MAX_IDENTICAL_READ_ONLY_CALLS + 2,
    ]
    assert [event.data["arguments"]["directory"] for event in refusals] == [
        ".//.",
        "  .  ",
    ]
    # The last spelling is never reached: refusing two in a row is itself no
    # progress, so the stall guard ends the call first.
    assert events.of_type("agent_stalled")


def test_genuine_work_in_many_rounds_is_never_called_a_stall(workspace):
    """The case DR-030 lifted the cap for must survive the new guard."""
    for index in range(40):
        (workspace.root / f"f{index}.txt").write_text(f"body {index}", encoding="utf-8")
    script = [
        calls(tool_call("read_file", path=f"f{index}.txt"), content="reading")
        for index in range(40)
    ] + [answer("finished")]
    agent, events = _stalling_agent(workspace, script)
    result = agent.run("read everything")

    assert result.stop_reason == "answered"
    assert result.content == "finished"
    assert result.rounds == 41
    assert not events.of_type("agent_stalled")


def test_re_reading_one_file_between_new_ones_is_not_a_stall(workspace):
    """A round that learns anything resets the count, even if it re-reads too."""
    for index in range(6):
        (workspace.root / f"f{index}.txt").write_text(f"body {index}", encoding="utf-8")
    script = [
        calls(
            tool_call("read_file", call_id="a", path="m.txt"),
            tool_call("read_file", call_id="b", path=f"f{index}.txt"),
            content="reading",
        )
        for index in range(6)
    ] + [answer("finished")]
    agent, events = _stalling_agent(workspace, script)
    result = agent.run("read around")

    assert result.stop_reason == "answered"
    assert not events.of_type("agent_stalled")


def test_unlimited_empty_turn_nudges_do_not_raise(workspace):
    """`None` is DR-030's "no limit"; it used to be compared against an int."""
    script = [answer(""), answer(""), answer("finally")]
    agent, _ = _stalling_agent(workspace, script, max_empty_turn_nudges=UNLIMITED)
    result = agent.run("say something")
    assert result.content == "finally"


# -- a stall that is really a context window ------------------------------------
#
# The failure this covers reached a user as "Stopped — Stalled" on `tell me
# about this project`: Ollama was serving a 262k-capable model in a 4,096-token
# window, the budget evicted each round's tool results to fit, and the model
# re-read the file it could no longer see. Blaming the model there sends the
# user to swap it, which cannot help.
#
# The shape matters. Repeating one call *identically* never evicts anything —
# the repeat guard refuses the third one and the refusal is two lines long. It
# is the model that respells its way around that tally, cycling a few real
# calls whose results it has already been given, that keeps filling the window
# while learning nothing. That is what the trace showed, and what these build.


def _cycling_reader(workspace, limit, files="abc"):
    """An agent that re-reads a few large files in rotation, forever."""
    for name in files:
        (workspace.root / f"{name}.txt").write_text(name * 40_000, encoding="utf-8")
    script = [
        calls(tool_call("read_file", path=f"{name}.txt"), content="looking")
        for name in itertools.islice(itertools.cycle(files), 60)
    ]
    events = RecordingEventSink()
    agent = _Stage(
        model=ScriptedChatModel(script),
        tools=catalog_subset("read_file", "list_files"),
        workspace=workspace,
        events=events,
        budget=ContextBudget(context_limit=limit),
    )
    return agent, events


def test_a_stall_caused_by_eviction_is_reported_as_the_window_not_the_model(workspace):
    """The no-progress guard must not blame a model the budget is starving."""
    agent, events = _cycling_reader(workspace, limit=4096)
    result = agent.run("tell me about this project")

    assert result.stop_reason == "context_thrashing"
    assert not events.of_type("agent_stalled")
    thrashing = events.of_type("agent_context_thrashing")
    assert thrashing
    assert "4,096-token context window" in thrashing[0].message
    # The advice has to point at the window, not at the model.
    assert "being made to forget" in thrashing[0].message
    assert thrashing[0].data["context_limit"] == 4096
    # Every one of the rounds it gave up over had lost history.
    assert all(
        metric.dropped_message_count
        for metric in result.context_metrics[-thrashing[0].data["stalled_rounds"] :]
    )


def test_a_stall_with_room_to_spare_is_still_blamed_on_the_model(workspace):
    """The new diagnosis must not swallow the old one.

    A model that loops in a window big enough to hold everything it read is
    genuinely going in circles, and `agent_stalled` is the honest verdict.
    """
    script = [
        calls(tool_call("read_file", path="m.txt"), content="still looking")
        for _ in range(60)
    ]
    agent, events = _stalling_agent(
        workspace, script, budget=ContextBudget(context_limit=128_000)
    )
    result = agent.run("explain this project")

    assert result.stop_reason == "stalled"
    stalled = events.of_type("agent_stalled")
    assert stalled
    assert not events.of_type("agent_context_thrashing")
    # Nothing was evicted, so nothing is said about the window.
    assert stalled[0].data["evicting_rounds"] == 0
    assert "context window" not in stalled[0].message


def test_a_stall_that_only_partly_evicted_still_mentions_the_window(workspace):
    """Between the two verdicts is a case that must not be reported as either.

    The model began looping while it still had what it had read, so the window
    is not the cause — but it is short enough to be making things worse, and a
    verdict that says only "the model went in circles" sends the user to change
    the one thing that was not the whole problem.
    """
    agent, events = _cycling_reader(workspace, limit=8192)
    result = agent.run("tell me about this project")

    assert result.stop_reason == "stalled"
    stalled = events.of_type("agent_stalled")
    assert stalled
    assert 0 < stalled[0].data["evicting_rounds"] < stalled[0].data["stalled_rounds"]
    assert "raising it may help as well" in stalled[0].message
