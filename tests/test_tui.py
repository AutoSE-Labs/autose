"""The terminal Adapter: it renders and gates, and decides nothing (DR-002)."""

from __future__ import annotations

import importlib
import importlib.util
import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from logic.common.allowances import Allowance
from logic.common.approval import ApprovalRequest
from logic.common.events import Event
from logic.common.workspace import Workspace
from logic.inference.catalog import ModelInfo
from logic.inference.discovery import DetectedRuntime
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.session import Session
from logic.tiers import CRAFT, SPARK
from tests.support.fakes import ScriptedChatModel, answer
from tui.activity import ActivityIndicator
from tui.approval import TerminalApprovalGate
from tui.cli import main, parse_args
from tui.commands import command_help, dispatch, is_command
from tui.keys import CTRL_O, KeyReader, strip_escapes
from tui.render import (
    answer_markdown,
    approval_panel,
    banner,
    denied_line,
    format_tokens,
    model_table,
    reasoning_transcript,
    runtime_table,
    status_line,
    thinking_block,
    thinking_hidden_line,
    thinking_summary,
    tier_table,
)

_MODEL = ModelInfo(name="gemma4:12b", context_length=262144, supports_tools=True)


def _console() -> Console:
    return Console(file=io.StringIO(), width=100, force_terminal=False, highlight=False)


def _render(renderable) -> str:
    console = _console()
    console.print(renderable)
    return console.file.getvalue()


@pytest.fixture
def session(tmp_path):
    def factory(settings):
        return ScriptedChatModel([answer("hi")])

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (_MODEL, ModelInfo(name="tiny:1b", supports_tools=False))

    register_provider(
        Provider(name="test-tui", chat_model=factory, catalog=_Catalog, is_local=True),
        replace=True,
    )
    return Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_MODEL.name, provider="test-tui"
        ),
        workspace=Workspace.at(tmp_path),
        model_info=_MODEL,
        auto_classify=False,
    )


class _App:
    """The minimum an ``App`` must expose for a command handler."""

    def __init__(self, session, console, gate):
        self.session = session
        self.console = console
        self.approval_gate = gate
        self.activity = ActivityIndicator(console)

    def print_status(self):
        self.console.print(status_line(self.session))


@pytest.fixture
def app(session):
    console = _console()
    return _App(session, console, TerminalApprovalGate(console))


# -- rendering ---------------------------------------------------------------


def test_the_banner_names_the_product_and_version():
    assert "AutoSE" in _render(banner("1.2.3")) and "1.2.3" in _render(banner("1.2.3"))


def test_the_status_line_shows_model_tier_workspace_and_context(session):
    rendered = _render(status_line(session))
    assert "gemma4:12b" in rendered
    assert SPARK in rendered
    assert "256k" in rendered


def test_the_model_table_marks_tool_capability(session):
    rendered = _render(
        model_table(
            (ModelInfo(name="a", supports_tools=True), ModelInfo(name="b", supports_tools=False)),
            current="a",
        )
    )
    assert "✓" in rendered and "✗" in rendered


def test_the_runtime_table_distinguishes_running_empty_and_absent():
    rendered = _render(
        runtime_table(
            (
                DetectedRuntime("ollama", "http://a/v1", True, (_MODEL,)),
                DetectedRuntime("vllm", "http://b/v1", True, ()),
                DetectedRuntime("lm-studio", "http://c/v1", False),
            )
        )
    )
    assert "ollama" in rendered and "none" in rendered and "—" in rendered


def test_the_tier_table_reports_readiness_for_every_tier(session):
    rendered = _render(tier_table(session))
    assert rendered.count("ready") == len(session.implemented_tiers)
    for tier in (SPARK, CRAFT):
        assert tier in rendered


def test_the_approval_panel_shows_the_exact_call():
    """DR-012: the concrete arguments, not a generic notice."""
    rendered = _render(
        approval_panel(ApprovalRequest("run_command", {"command": "rm -rf build"}))
    )
    assert "run_command" in rendered
    assert "rm -rf build" in rendered


def test_the_approval_panel_spells_out_every_answer():
    """The prompt used to show only its default, which read as the only option."""
    rendered = _render(approval_panel(ApprovalRequest("run_command", {"command": "ls"})))
    assert "[y]" in rendered and "[n]" in rendered and "[a]" in rendered
    assert "type a reason" in rendered


def test_the_approval_panel_shows_arguments_the_summary_omits():
    rendered = _render(
        approval_panel(ApprovalRequest("fetch_url", {"url": "http://x", "timeout": 30}))
    )
    assert "http://x" in rendered and "timeout" in rendered and "30" in rendered


def test_a_denial_carries_its_reason_into_the_transcript():
    assert "use ripgrep" in _render(denied_line("declined: use ripgrep instead"))


def test_an_empty_answer_still_renders_something():
    assert _render(answer_markdown("")).strip()


@pytest.mark.parametrize(
    ("count", "expected"), [(5, "5"), (1500, "1.5k"), (2_500_000, "2.5M")]
)
def test_token_counts_are_abbreviated(count, expected):
    assert format_tokens(count) == expected


# -- the approval gate --------------------------------------------------------


def test_approving_once_does_not_grant_a_standing_allowance(monkeypatch):
    console = _console()
    gate = TerminalApprovalGate(console)
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "y")
    assert gate.request(ApprovalRequest("run_command", {"command": "ls"})).approved
    assert gate.always_allowed == ()


def test_a_stopping_run_denies_without_prompting(monkeypatch):
    """DR-030: ctrl-c at an approval prompt must not look like it did nothing.

    The run is blocked on the prompt and cannot reach a point where it would
    notice the stop, so the gate answers for the user.
    """
    stopping = False
    gate = TerminalApprovalGate(_console(), stop_requested=lambda: stopping)
    monkeypatch.setattr(
        "tui.approval.Prompt.ask",
        lambda *a, **k: pytest.fail("a stopping run must not be asked"),
    )
    # Even a standing allowance does not run the call.
    # Reaches into the standing-allow set directly: that is the state under test.
    gate._always_allow["run_command:ls"] = Allowance("run_command", "ls")
    stopping = True

    decision = gate.request(ApprovalRequest("run_command", {"command": "ls"}))

    assert not decision.approved
    assert "stopped this run" in decision.reason


def test_denying_returns_a_denial(monkeypatch):
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "n")
    assert not gate.request(ApprovalRequest("run_command", {"command": "ls"})).approved


def test_the_default_answer_approves_once(monkeypatch):
    """Enter at the prompt means yes — the panel says so, so it is informed."""
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "")
    decision = gate.request(ApprovalRequest("run_command", {"command": "ls"}))
    assert decision.approved and gate.always_allowed == ()


@pytest.mark.parametrize(
    ("reply", "approved"),
    [("yes", True), ("no", False), ("always", True), ("skip", False), ("Y", True)],
)
def test_whole_words_answer_the_prompt_too(monkeypatch, reply, approved):
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: reply)
    assert gate.request(ApprovalRequest("run_command", {"command": "ls"})).approved is approved


def test_free_text_declines_and_reaches_the_model_as_the_reason(monkeypatch):
    """A refusal that carries guidance redirects the run instead of stopping it."""
    from logic.common.approval import denial_result

    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr(
        "tui.approval.Prompt.ask", lambda *a, **k: "use ripgrep instead"
    )
    request = ApprovalRequest("run_command", {"command": "grep -r x ."})
    decision = gate.request(request)
    assert not decision.approved
    assert "use ripgrep instead" in decision.reason
    assert "use ripgrep instead" in denial_result(request, decision)


def test_free_text_never_grants_a_standing_allowance(monkeypatch):
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "always be careful")
    assert not gate.request(ApprovalRequest("run_command", {"command": "ls"})).approved
    assert gate.always_allowed == ()


def test_always_allow_covers_calls_like_this_one_only(monkeypatch):
    """DR-048: "a" on `pytest` allows pytest, not every shell command."""
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "a")
    gate.request(ApprovalRequest("run_command", {"command": "pytest -q"}))
    assert gate.always_allowed == ("pytest … commands",)

    # Another pytest call is not prompted for again.
    monkeypatch.setattr(
        "tui.approval.Prompt.ask", lambda *a, **k: pytest.fail("should not prompt")
    )
    assert gate.request(ApprovalRequest("run_command", {"command": "pytest tests/x.py"})).approved

    # A different command, and a different approval-required tool, still ask.
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "n")
    assert not gate.request(ApprovalRequest("run_command", {"command": "ls"})).approved
    assert not gate.request(ApprovalRequest("run_command", {"command": "pytest; ls"})).approved
    assert not gate.request(ApprovalRequest("fetch_url", {"url": "http://x"})).approved


def test_always_allow_on_a_command_that_cannot_stand_runs_it_once(monkeypatch):
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "a")
    assert gate.request(ApprovalRequest("run_command", {"command": "rm -rf build"})).approved
    assert gate.always_allowed == ()


def test_an_interrupted_prompt_denies(monkeypatch):
    gate = TerminalApprovalGate(_console())

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("tui.approval.Prompt.ask", interrupt)
    decision = gate.request(ApprovalRequest("run_command", {"command": "ls"}))
    assert not decision.approved
    assert "interrupted" in decision.reason


def test_resetting_clears_standing_allowances(monkeypatch):
    gate = TerminalApprovalGate(_console())
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "a")
    gate.request(ApprovalRequest("run_command", {"command": "ls"}))
    gate.reset()
    assert gate.always_allowed == ()


# -- commands ------------------------------------------------------------------


def test_slash_prefix_identifies_a_command():
    assert is_command("/help") and not is_command("what is this?")


def test_every_command_is_documented():
    assert "/help" in command_help() and "/model" in command_help()


def test_quit_ends_the_session(app):
    assert dispatch(app, "/quit") is False


def test_an_unknown_command_is_reported_not_fatal(app):
    assert dispatch(app, "/nope") is True
    assert "unknown command" in app.console.file.getvalue()


def test_model_without_an_argument_lists(app):
    dispatch(app, "/model")
    assert "gemma4:12b" in app.console.file.getvalue()


def test_model_switches_by_index_and_by_name(app):
    dispatch(app, "/model 2")
    assert app.session.inference.model == "tiny:1b"
    dispatch(app, "/model gemma4:12b")
    assert app.session.inference.model == "gemma4:12b"


def test_switching_to_a_tool_incapable_model_warns(app):
    dispatch(app, "/model tiny:1b")
    assert "no tool-calling support" in app.console.file.getvalue()


def test_an_unmatched_model_is_reported(app):
    dispatch(app, "/model nonexistent")
    assert "no model matching" in app.console.file.getvalue()


def test_switching_models_clears_standing_approvals(app, monkeypatch):
    monkeypatch.setattr("tui.approval.Prompt.ask", lambda *a, **k: "a")
    app.approval_gate.request(ApprovalRequest("run_command", {"command": "ls"}))
    dispatch(app, "/model 2")
    assert app.approval_gate.always_allowed == ()


def test_tier_goes_up_and_back_down_when_chosen(app):
    """DR-049: a tier the person picks is used as picked."""
    dispatch(app, f"/tier {CRAFT}")
    assert app.session.tier == CRAFT
    dispatch(app, f"/tier {SPARK}")
    assert app.session.tier == SPARK
    assert "tier is now spark" in app.console.file.getvalue()


def test_requesting_an_implemented_tier_confirms_the_change(app):
    dispatch(app, f"/tier {CRAFT}")
    assert app.session.tier == CRAFT
    assert "tier is now craft" in app.console.file.getvalue()


def test_a_retired_tier_name_is_rejected(app):
    dispatch(app, "/tier advanced")
    assert "is not a tier" in app.console.file.getvalue()


def test_tools_lists_which_calls_are_gated(app):
    dispatch(app, "/tools")
    output = app.console.file.getvalue()
    assert "run_command" in output and "asks first" in output


def test_clear_forgets_history(app):
    app.session.run("hello")
    dispatch(app, "/clear")
    assert app.session.history == ()


def test_status_prints_the_status_line(app):
    dispatch(app, "/status")
    assert "gemma4:12b" in app.console.file.getvalue()


# -- the activity indicator ------------------------------------------------------


def test_the_indicator_does_not_animate_when_output_is_not_a_terminal():
    """A spinner is meaningful in a terminal and noise in a CI log."""
    indicator = ActivityIndicator(_console())
    indicator.start("thinking")
    assert indicator._live is None
    indicator.stop()


def test_elapsed_time_spans_the_whole_turn_across_an_approval():
    indicator = ActivityIndicator(_console())
    indicator.start("thinking")
    started = indicator.elapsed
    indicator.emit(Event("approval_requested"))
    indicator.emit(Event("approval_resolved", data={"approved": True}))
    assert indicator.elapsed >= started


def test_a_completed_tool_is_reported_with_its_size():
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.emit(
        Event("tool_completed", data={"tool": "read_file", "result_chars": 1234})
    )
    assert "read_file" in console.file.getvalue()
    assert "1,234" in console.file.getvalue()


def test_a_preserved_oversized_result_shows_its_recovery_path():
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.emit(
        Event(
            "tool_completed",
            data={
                "tool": "run_command",
                "result_chars": 99_000,
                "truncated": True,
                "artifact": ".autose/tool-output/run_command-abc.txt",
            },
        )
    )
    assert ".autose/tool-output/run_command-abc.txt" in console.file.getvalue()


def test_a_denied_call_is_shown():
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.emit(Event("approval_resolved", data={"approved": False}))
    assert "denied" in console.file.getvalue()


def test_unknown_events_are_ignored():
    ActivityIndicator(_console()).emit(Event("something_new", data={}))


def test_animation_follows_the_real_tty_not_the_colour_setting(monkeypatch):
    """FORCE_COLOR makes Rich claim a terminal; that must not start a spinner."""
    console = Console(file=io.StringIO(), width=100, force_terminal=True)
    assert console.is_terminal  # what FORCE_COLOR produces
    indicator = ActivityIndicator(console)
    indicator.start("thinking")
    assert indicator._live is None
    indicator.stop()


# -- keys pressed while the model works ---------------------------------------------


class _ScriptedKeys:
    """A ``KeyReader`` whose keypresses come from a string, not a terminal."""

    def __init__(self, script: str = "") -> None:
        self.script = script
        self.active = False

    @property
    def available(self) -> bool:
        return True

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def read(self) -> str:
        if not self.active:
            return ""
        script, self.script = self.script, ""
        return script


def _pressing(script: str) -> ActivityIndicator:
    keys = _ScriptedKeys(script)
    indicator = ActivityIndicator(_console(), keys=keys)
    keys.start()
    indicator.start()
    indicator.emit(Event("agent_reasoning_delta", "reasoning about the parser"))
    return indicator


def test_ctrl_o_cycles_the_block_while_the_model_is_still_working():
    """The moment you want more of a thought is while it is still streaming."""
    indicator = _pressing(CTRL_O)
    indicator.pump_keys()
    assert indicator.show_thinking and indicator.expanded

    indicator._keys.script = CTRL_O
    indicator.pump_keys()
    assert not indicator.show_thinking  # hidden

    indicator._keys.script = CTRL_O
    indicator.pump_keys()
    assert indicator.show_thinking and not indicator.expanded  # back to collapsed


def test_a_hidden_block_still_says_how_to_bring_it_back():
    indicator = _pressing(CTRL_O + CTRL_O)
    indicator.pump_keys()
    assert not indicator.show_thinking
    assert "ctrl-o to show" in _render(indicator._thought_block(80))


def test_the_hint_offers_the_command_when_no_key_can_be_read():
    """Promising a shortcut a piped session cannot deliver would be a lie."""
    indicator = ActivityIndicator(_console())  # a real reader, over a StringIO
    indicator.start()
    indicator.emit(Event("agent_reasoning_delta", "thinking"))
    assert not indicator.keys_captured
    assert "/thinking to read it all" in _render(indicator._thought_block(80))


def test_typing_ahead_is_kept_and_shown_rather_than_swallowed():
    """Keys are read one at a time, so the terminal is not echoing them."""
    indicator = _pressing("fix the parser next")
    indicator.pump_keys()
    assert "fix the parser next" in _render(indicator._queued_input())
    assert indicator.take_typed() == "fix the parser next"
    assert indicator.take_typed() == ""


def test_type_ahead_survives_a_shortcut_and_a_backspace():
    indicator = _pressing(f"abc\x7f{CTRL_O}d")
    indicator.pump_keys()
    assert indicator.take_typed() == "abd"
    assert indicator.expanded


def test_a_blind_enter_does_not_submit_anything():
    """The user cannot see what they typed; it is confirmed at the prompt."""
    indicator = _pressing("run the tests\r")
    indicator.pump_keys()
    assert indicator.take_typed() == "run the tests"


def test_arrow_keys_are_not_carried_into_the_prompt():
    indicator = _pressing("a\x1b[Ab")
    indicator.pump_keys()
    assert indicator.take_typed() == "ab"


def test_escape_sequences_are_stripped_not_the_text_around_them():
    assert strip_escapes("a\x1b[2Db\x1bOPc") == "abc"


def test_a_key_reader_over_a_pipe_is_inert():
    reader = KeyReader(io.StringIO())
    assert not reader.available
    reader.start()
    assert not reader.active and reader.read() == ""
    reader.stop()


def test_the_indicator_releases_the_terminal_when_it_stops():
    """An approval prompt must never find the terminal in cbreak mode."""
    keys = _ScriptedKeys()
    indicator = ActivityIndicator(_console(), keys=keys)
    keys.start()
    indicator.emit(Event("approval_requested"))
    assert not keys.active


# -- reading a request --------------------------------------------------------------


def _app(session, replies):
    from tui.app import App

    console = _console()
    console.input = lambda *args, **kwargs: replies.pop(0)
    return App(
        session,
        console=console,
        approval_gate=TerminalApprovalGate(console),
        activity=ActivityIndicator(console),
    )


def test_a_trailing_backslash_continues_the_request(session):
    """A task worth describing is often worth more than one line."""
    app = _app(session, ["fix the parser: \\", "it drops the last token", ""])
    assert app._read() == "fix the parser:\nit drops the last token"


def test_a_single_line_request_is_unaffected(session):
    assert _app(session, ["  hello  "])._read() == "hello"


def test_interrupting_a_continued_request_discards_it(session):
    """Ctrl-c mid-request throws the draft away; it never submits a half line."""
    app = _app(session, [])
    replies = ["first line \\"]

    def scripted(*args, **kwargs):
        if replies:
            return replies.pop(0)
        raise KeyboardInterrupt

    app.console.input = scripted
    assert app._read() == ""
    assert "discarded" in app.console.file.getvalue()


def test_what_was_typed_during_a_turn_reaches_the_next_prompt(session):
    """Typing ahead is not punished: it arrives pre-filled, and editable."""
    app = _app(session, [""])
    app.activity._typed = "and then run the tests"
    prefills = []
    app._input = lambda prompt, prefill="": (prefills.append(prefill) or "")
    app._end_activity()
    app._read()
    assert prefills == ["and then run the tests"]
    # And only once: the next prompt starts clean.
    app._read()
    assert prefills == ["and then run the tests", ""]


def test_end_of_input_ends_the_session(session):
    def eof(*args, **kwargs):
        raise EOFError

    app = _app(session, [])
    app.console.input = eof
    assert app._read() is None


# -- the thinking block ------------------------------------------------------------


def test_the_thinking_block_keeps_the_tail_within_its_line_budget():
    """The point of a block is that a whole sentence stays readable."""
    thought = " ".join(f"word{n}" for n in range(400))
    rendered = _render(thinking_block(thought, width=60, max_lines=3))
    body = [line for line in rendered.splitlines() if "│" in line]
    assert len(body) == 3
    assert "word399" in rendered  # the newest text, not the oldest
    assert "word0 " not in rendered


def test_an_expanded_block_shows_more_lines():
    thought = " ".join(f"word{n}" for n in range(400))
    collapsed = _render(thinking_block(thought, width=60, max_lines=3))
    expanded = _render(thinking_block(thought, width=60, max_lines=12, expanded=True))
    assert len(expanded.splitlines()) > len(collapsed.splitlines())


def test_the_block_says_what_can_be_done_about_it():
    thought = "some reasoning"
    assert "ctrl-o to expand" in _render(
        thinking_block(thought, width=60, max_lines=3, hint="ctrl-o to expand")
    )
    assert "ctrl-o to show" in _render(thinking_hidden_line("ctrl-o to show"))


def test_the_summary_line_says_how_to_read_the_whole_thought():
    assert "/thinking" in _render(thinking_summary(1234, 12.0))


def test_the_transcript_reports_when_there_was_no_reasoning():
    assert "nothing was reasoned" in _render(reasoning_transcript(()))


def test_streamed_reasoning_is_kept_whole_and_summarised_in_one_line():
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.start()
    for part in ("Checking the config ", "loader, then the TUI override."):
        indicator.emit(Event("agent_reasoning_delta", part))
    indicator.finish_turn()

    assert indicator.thoughts == (
        ("thinking", "Checking the config loader, then the TUI override."),
    )
    output = console.file.getvalue()
    assert "thought for" in output and "/thinking" in output
    # The thought itself is not spilled into the transcript, only the summary.
    assert "Checking the config" not in output


def test_the_thought_block_is_suppressed_when_thinking_is_turned_off():
    indicator = ActivityIndicator(_console())
    indicator.start()
    indicator.emit(Event("agent_reasoning_delta", "some reasoning"))
    assert indicator._thought_block(80) is not None
    indicator.show_thinking = False
    assert indicator._thought_block(80) is None


def test_a_new_turn_forgets_the_previous_turn_s_thoughts():
    indicator = ActivityIndicator(_console())
    indicator.start()
    indicator.emit(Event("agent_reasoning", "first"))
    indicator.finish_turn()
    indicator.start()
    assert indicator.thoughts == ()


def test_tool_arguments_stay_on_the_status_line_not_in_the_thought():
    """A path being read is not reasoning, and must not pollute /thinking."""
    indicator = ActivityIndicator(_console())
    indicator.start()
    indicator.emit(Event("tool_called", data={"tool": "read_file", "arguments": {"path": "a.py"}}))
    assert indicator.thoughts == ()
    assert "a.py" in indicator._frame().plain


def test_thinking_prints_the_last_turn_in_full(app):
    app.activity.start()
    app.activity.emit(Event("agent_reasoning", "the whole chain of thought"))
    dispatch(app, "/thinking")
    assert "the whole chain of thought" in app.console.file.getvalue()


def test_thinking_can_be_turned_off_and_expanded(app):
    dispatch(app, "/thinking off")
    assert app.activity.show_thinking is False
    dispatch(app, "/thinking expand")
    assert app.activity.show_thinking is True and app.activity.expanded is True
    dispatch(app, "/thinking collapse")
    assert app.activity.expanded is False
    dispatch(app, "/thinking sideways")
    assert "is not an option" in app.console.file.getvalue()


def test_a_denied_call_shows_the_reason_it_was_given():
    console = _console()
    ActivityIndicator(console).emit(
        Event("approval_resolved", data={"approved": False, "reason": "use ripgrep"})
    )
    assert "use ripgrep" in console.file.getvalue()


def test_a_mistyped_command_is_offered_the_one_that_was_meant(app):
    dispatch(app, "/thinkng")
    assert "did you mean /thinking?" in app.console.file.getvalue()


# -- multi-stage rendering ---------------------------------------------------------


def test_the_verdict_panel_shows_an_overridden_pass_plainly():
    """DR-005's whole point is visible when the runner overrides a claimed pass."""
    from tui.render import verdict_panel

    rendered = _render(
        verdict_panel(
            {"passed": False, "overridden": True, "unmet": ["R2"], "feedback": "R2 has no test"}
        )
    )
    assert "claimed pass overridden" in rendered
    assert "R2" in rendered


def test_the_verdict_panel_shows_a_plain_rejection_differently():
    from tui.render import verdict_panel

    rendered = _render(
        verdict_panel({"passed": False, "overridden": False, "target": "execute", "feedback": "x"})
    )
    assert "sent back" in rendered and "execute" in rendered


def test_a_passing_verdict_reads_as_a_pass():
    from tui.render import verdict_panel

    assert "passed" in _render(verdict_panel({"passed": True, "requirements": "2 verified"}))


def test_the_requirements_table_colours_each_status():
    from tui.render import requirements_table

    rendered = _render(
        requirements_table(
            [
                {"id": "R1", "description": "a", "status": "Verified", "evidence": "x.py"},
                {"id": "R2", "description": "b", "status": "Blocked", "evidence": ""},
            ]
        )
    )
    assert "R1" in rendered and "Verified" in rendered and "Blocked" in rendered


def test_an_exhausted_bound_is_never_rendered_as_a_success():
    """DR-009: exhaustion gets its own panel, not an answer."""
    from tui.render import outcome_panel

    rendered = _render(outcome_panel("attempts_exhausted", "Validation sent it back 3 times."))
    assert "Out of attempts" in rendered
    assert "sent it back 3 times" in rendered

    rendered = _render(outcome_panel("time_budget_exhausted", "The budget ran out."))
    assert "Out of time" in rendered

    # DR-030: a run the user ended reads as their choice, not as a fault, and
    # says something even when it stopped before writing anything.
    rendered = _render(outcome_panel("cancelled", "Stopped at your request."))
    assert "Stopped by you" in rendered
    assert "Stopped at your request" in rendered

    rendered = _render(outcome_panel("cancelled", ""))
    assert "Stopped by you" in rendered
    assert "Stopped before" in rendered

    # An answer already streamed to the screen is pointed at, not printed twice.
    rendered = _render(
        outcome_panel("cancelled", "half an answer", already_shown=True)
    )
    assert "half an answer" not in rendered
    assert "as far as it got" in rendered


def test_the_classification_line_shows_a_ratchet_that_overrode_it():
    from tui.render import classification_line

    rendered = _render(classification_line("spark", "model", "craft"))
    assert "spark" in rendered and "craft" in rendered and "ratchets up" in rendered


def test_a_low_confidence_classification_is_flagged():
    from tui.render import classification_line

    assert "text_scan" in _render(classification_line("craft", "text_scan"))


def test_stage_headers_show_position_and_retry_attempt():
    from tui.render import stage_header

    assert "2/3" in _render(stage_header("execute", "Execute", 1, total_stages=3, position=2))
    assert "attempt 2" in _render(stage_header("execute", "Execute", 2))


def test_the_activity_sink_renders_a_whole_staged_run():
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.start()
    for event in (
        Event("workflow_started", "", {"tier": "craft", "stages": ["plan", "execute", "validate"]}),
        Event("stage_started", "Building the plan", {"stage": "plan", "title": "Plan", "iteration": 1}),
        Event(
            "stage_finished",
            "Plan",
            {"stage": "plan", "requirements": [{"id": "R1", "description": "a", "status": "Not Started", "evidence": ""}]},
        ),
        Event("stage_retry", "bad ledger", {"stage": "execute", "reason": "bad ledger"}),
        Event("validation_verdict", "", {"passed": True, "requirements": "1 verified"}),
        Event("loopback", "", {"target": "execute", "iteration": 1, "cap": 3}),
    ):
        indicator.emit(event)
    indicator.stop()

    output = console.file.getvalue()
    assert indicator.staged
    assert "plan → execute → validate" in output
    assert "Plan" in output and "R1" in output
    assert "bad ledger" in output
    assert "↻ back to execute" in output


def test_a_nudge_is_shown_rather_than_a_silent_hang():
    """DR-019: the model going quiet must be visible, not indistinguishable
    from a hang."""
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.start()
    indicator.emit(
        Event(
            "agent_nudged",
            "asked for the final answer",
            {"stage": "execute", "attempt": 1, "gathered_evidence": True},
        )
    )
    indicator.stop()

    output = console.file.getvalue()
    assert "asked for the final answer" in output
    assert "attempt 1" in output


def test_yes_and_workspace_preserve_tuning_and_prompt_overrides(monkeypatch):
    """--yes / --workspace must not silently reset DR-021's tuning and prompt
    overrides back to their defaults."""
    from pathlib import Path

    from logic.config import AutoSEConfig
    from logic.tuning import ShellTuning, Tuning
    from tui import app as tui_app

    base_config = AutoSEConfig(
        tuning=Tuning(shell=ShellTuning(command_timeout_seconds=999.0)),
        prompt_overrides={"classifier_system": "custom"},
    )
    monkeypatch.setattr(tui_app, "load_config", lambda path, **kwargs: base_config)

    captured: dict = {}

    def fake_resolve(console, config):
        captured["config"] = config
        return None

    monkeypatch.setattr(tui_app, "_resolve", fake_resolve)

    tui_app.run(auto_approve=True, workspace="/tmp/other-repo", console=_console())

    config = captured["config"]
    assert config.tuning.shell.command_timeout_seconds == 999.0
    assert config.prompt_overrides == {"classifier_system": "custom"}
    assert config.workspace_root == Path("/tmp/other-repo")
    assert config.auto_approve is True


def test_the_tier_is_printed_as_soon_as_it_is_known():
    """Before the first stage runs, not after it has gone by."""
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.start()
    indicator.emit(
        Event(
            "classification_finished",
            "craft",
            {"tier": "craft", "effective_tier": "craft", "ratcheted": False, "source": "model"},
        )
    )
    assert "craft" in console.file.getvalue()


def test_a_ratcheted_tier_is_shown_as_a_transition():
    console = _console()
    indicator = ActivityIndicator(console)
    indicator.start()
    indicator.emit(
        Event(
            "classification_finished",
            "craft",
            {"tier": "spark", "effective_tier": "craft", "ratcheted": True, "source": "model"},
        )
    )
    output = console.file.getvalue()
    assert "spark" in output and "craft" in output and "ratchets up" in output


# -- /config: DR-021's configurability has to be reachable -------------------


def test_config_writes_a_template_that_loads_back(app, tmp_path):
    """A config nobody can create configures nothing."""
    from logic.config import load_config

    assert dispatch(app, "/config") is True

    written = tmp_path / ".autose" / "config.yaml"
    assert written.is_file()
    # Round-trips: the shipped defaults are valid input, not just documentation.
    assert load_config(workspace_root=tmp_path).inference.provider


def test_config_refuses_to_overwrite_an_existing_file(app, tmp_path):
    """Someone's edited settings are not a thing to silently replace."""
    dispatch(app, "/config")
    written = tmp_path / ".autose" / "config.yaml"
    written.write_text("inference:\n  model: mine\n", encoding="utf-8")

    dispatch(app, "/config")

    assert written.read_text(encoding="utf-8") == "inference:\n  model: mine\n"


def test_config_is_listed_in_help():
    assert "/config" in command_help()


# -- the --web hand-off (DR-013, DR-029) ---------------------------------------


def test_web_is_offered_by_the_terminal_cli():
    """The dashboard is reachable from the command people already type."""
    args = parse_args(["--web", "--no-browser", "--port", "0"])

    assert args.web is True
    assert args.open_browser is False
    assert args.port == 0


def test_web_hands_off_to_the_dashboard_with_the_shared_flags_forwarded(monkeypatch):
    """One parser's answers, forwarded — not a second copy of the defaults."""
    received: dict = {}

    def fake_main(argv):
        received["argv"] = argv
        return 0

    # Patched on the module objects, not by dotted string: monkeypatch resolves
    # a string target through `importlib` itself, which this test is replacing.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(
        importlib, "import_module", lambda name: SimpleNamespace(main=fake_main)
    )

    code = main(
        ["--web", "--yes", "--tier", CRAFT, "--port", "0", "--no-browser", "--resume", "abc"]
    )

    assert code == 0
    assert received["argv"] == [
        "--tier",
        CRAFT,
        "--yes",
        "--resume",
        "abc",
        "--port",
        "0",
        "--no-browser",
    ]


def test_web_explains_itself_when_the_build_has_no_dashboard(monkeypatch, capsys):
    """A build without src/gui must say so for --web, not crash (DR-029)."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    code = main(["--web"])

    assert code == 2
    assert "no web dashboard" in capsys.readouterr().err


def test_web_refuses_to_swallow_a_prompt_typed_on_the_command_line(capsys):
    """A request the user typed is never silently dropped on the floor."""
    code = main(["--web", "fix the parser"])

    assert code == 2
    error = capsys.readouterr().err
    assert "in the browser" in error and 'autose "fix the parser"' in error



# -- DR-030: ctrl-c stops the run, twice aborts it -----------------------------


def test_the_first_interrupt_asks_the_run_to_stop_and_the_second_aborts():
    """The gesture that replaces the caps DR-030 removed."""
    import signal

    from tui.interrupt import stop_on_interrupt

    asked: list[bool] = []
    announced: list[bool] = []
    before = signal.getsignal(signal.SIGINT)

    def request_stop() -> bool:
        asked.append(True)
        return len(asked) == 1

    with (
        pytest.raises(KeyboardInterrupt),
        stop_on_interrupt(request_stop, announce=announced.append),
    ):
        signal.raise_signal(signal.SIGINT)  # first: a polite stop
        assert asked == [True]
        assert announced == [True]
        signal.raise_signal(signal.SIGINT)  # second: abort

    # The handler is always handed back, or the next turn would inherit it.
    assert signal.getsignal(signal.SIGINT) is before


def test_a_stop_where_signals_are_unavailable_is_a_no_op(monkeypatch):
    import threading

    from tui.interrupt import stop_on_interrupt

    monkeypatch.setattr(
        threading, "main_thread", lambda: object(), raising=True
    )
    with stop_on_interrupt(lambda: True):
        pass  # nothing installed, nothing raised, nothing restored


def test_the_session_list_says_when_requests_wait_in_the_dashboard():
    """DR-056: a resume from the terminal keeps them; the list says they are there."""
    from logic.persistence import QueuedRequest, SessionRecord
    from tui.render import session_table

    waiting = SessionRecord(
        id="s1",
        queue=(QueuedRequest(id="q1", prompt="one"), QueuedRequest(id="q2", prompt="two")),
    )
    quiet = SessionRecord(id="s2")

    output = _render(session_table((waiting, quiet)))

    assert "2 queued in the dashboard" in output
    assert output.count("queued") == 1
