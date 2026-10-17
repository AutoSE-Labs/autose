"""The public facade every Adapter drives (DR-002)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.config import AutoSEConfig
from logic.errors import ConfigurationError
from logic.inference.catalog import ModelInfo
from logic.inference.discovery import DetectedRuntime
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.limits import UNLIMITED
from logic.session import Session, setup_from_config
from logic.tiers import CRAFT, SPARK
from logic.tuning import TierBounds, Tuning
from tests.support.fakes import (
    RecordingApprovalGate,
    ScriptedChatModel,
    answer,
    calls,
    tool_call,
)

_GEMMA = ModelInfo(
    name="gemma4:12b", context_length=262144, supports_tools=True, parameter_size="11.9B"
)
_TINY = ModelInfo(name="tiny:1b", context_length=4096, supports_tools=False)


@pytest.fixture
def scripted(monkeypatch):
    """Register a provider whose chat model is scripted by the test."""
    holder: dict = {}

    def factory(settings):
        return holder["model"]

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return holder.get("models", ())

    register_provider(
        Provider(name="test-session", chat_model=factory, catalog=_Catalog, is_local=True),
        replace=True,
    )
    return holder


def _session(scripted, tmp_path, script, *, models=(_GEMMA,), **kwargs) -> Session:
    scripted["model"] = ScriptedChatModel(script)
    scripted["models"] = models
    kwargs.setdefault("auto_classify", False)
    return Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_GEMMA.name, provider="test-session"
        ),
        workspace=Workspace.at(tmp_path),
        model_info=_GEMMA,
        **kwargs,
    )


# -- running ----------------------------------------------------------------


def test_a_prompt_runs_the_tier_workflow_and_returns_a_turn(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("the answer")])
    turn = session.run("what is this?")
    assert turn.answer == "the answer"
    assert turn.tier == SPARK
    assert turn.succeeded


def test_streaming_yields_text_then_exposes_the_turn(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("streamed")])
    stream = session.stream("go")
    assert "".join(stream) == "streamed"
    assert stream.turn.answer == "streamed"


def test_history_carries_forward_as_session_context(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("first"), answer("second")])
    session.run("one")
    session.run("two")
    model = scripted["model"]
    second_request = model.requests[1]["messages"]
    contents = [message.get("content") for message in second_request]
    assert "one" in contents and "first" in contents and "two" in contents


def test_a_turn_that_did_not_complete_is_not_fed_back_as_history(scripted, tmp_path):
    """A round-capped/cancelled answer is a partial draft, not a real answer.

    Recording it into session memory anyway hands the next prompt a prior
    'assistant' turn that trails off mid-thought, which is what pulls a small
    local model into resuming that abandoned reasoning instead of treating an
    unrelated next prompt as a fresh start.
    """
    session = _session(
        scripted,
        tmp_path,
        [calls(tool_call("read_file", path="m.txt"), content="still looking")] * 5
        + [answer("hi there")],
        tuning=Tuning(spark=TierBounds(max_tool_rounds=1)),
    )
    (tmp_path / "m.txt").write_text("body", encoding="utf-8")

    first = session.run("tell me about this project")
    assert first.stop_reason == "round_cap"

    session.run("hello")
    second_request = scripted["model"].requests[-1]["messages"]
    contents = [str(message.get("content")) for message in second_request]
    assert not any("tell me about this project" in content for content in contents)
    assert not any("still looking" in content for content in contents)


def test_only_the_final_pair_is_promoted_never_tool_transcripts(scripted, tmp_path):
    """DR-004: a tool-call transcript must not reach session memory."""
    (tmp_path / "m.txt").write_text("body", encoding="utf-8")
    session = _session(
        scripted,
        tmp_path,
        [calls(tool_call("read_file", path="m.txt")), answer("done"), answer("next")],
    )
    session.run("read it")
    session.run("and now?")
    second_request = scripted["model"].requests[-1]["messages"]
    assert not any(message.get("role") == "tool" for message in second_request)
    assert not any("body" in str(message.get("content")) for message in second_request)


def test_verbatim_history_is_bounded_and_the_rest_is_compressed(scripted, tmp_path):
    """DR-004: recent exchanges verbatim, everything older in one rolling summary."""
    # Two calls per turn once compression kicks in: the turn, then the summary.
    session = _session(scripted, tmp_path, [answer(f"a{i}") for i in range(40)])
    for index in range(10):
        session.run(f"prompt {index}")

    memory = session.memory
    assert len(memory.exchanges) <= memory.MAX_VERBATIM_EXCHANGES
    assert memory.summary, "older exchanges should have been compressed, not dropped"
    assert len(memory.summary) <= memory.MAX_SUMMARY_CHARS
    assert memory.dropped == 0



def test_clearing_history_drops_the_context(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("a"), answer("b")])
    session.run("one")
    session.clear_history()
    session.run("two")
    assert [m.get("content") for m in scripted["model"].requests[1]["messages"]] == [
        scripted["model"].requests[1]["messages"][0]["content"],
        "two",
    ]


def test_usage_accumulates_across_turns(scripted, tmp_path):
    session = _session(
        scripted,
        tmp_path,
        [answer("a", prompt_tokens=10, completion_tokens=1), answer("b", prompt_tokens=20, completion_tokens=2)],
    )
    session.run("one")
    session.run("two")
    assert session.usage.total_tokens == 33


# -- tiers -------------------------------------------------------------------


def test_the_session_tier_ratchets_and_never_downgrades(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("a")])
    assert session.tier == SPARK
    assert session.request_tier(CRAFT) == CRAFT
    assert session.request_tier(SPARK) == CRAFT


def test_every_tier_the_session_offers_can_actually_run(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("a")])
    assert set(session.implemented_tiers) == {SPARK, CRAFT}


def test_a_tier_change_is_announced_as_an_event(scripted, tmp_path):
    events = RecordingEventSink()
    session = _session(scripted, tmp_path, [answer("a")], events=events)
    session.request_tier(CRAFT)
    assert events.of_type("tier_changed")[0].data["tier"] == CRAFT


def test_turn_boundaries_are_emitted_as_events(scripted, tmp_path):
    events = RecordingEventSink()
    session = _session(scripted, tmp_path, [answer("a")], events=events)
    session.run("go")
    assert events.of_type("turn_started")[0].data["tier"] == SPARK
    assert events.of_type("turn_finished")[0].data["succeeded"] is True


# -- models -------------------------------------------------------------------


def test_switching_models_adopts_the_new_context_limit(scripted, tmp_path):
    session = _session(scripted, tmp_path, [answer("a")], models=(_GEMMA, _TINY))
    assert session.context_limit() == 262144
    session.select_model("tiny:1b")
    assert session.inference.model == "tiny:1b"
    assert session.context_limit() == 4096


def test_a_session_cannot_start_without_a_model(tmp_path):
    with pytest.raises(ConfigurationError, match="no model selected"):
        Session(
            inference=InferenceSettings(base_url="http://test/v1"),
            workspace=Workspace.at(tmp_path),
        )


def test_the_approval_gate_defaults_to_denying(scripted, tmp_path):
    """DR-012: an Adapter that supplies no gate gets refusals."""
    marker = tmp_path / "ran.txt"
    session = _session(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="touch ran.txt")), answer("blocked")],
    )
    session.run("go")
    assert not marker.exists()


def test_an_adapter_supplied_gate_is_used(scripted, tmp_path):
    gate = RecordingApprovalGate(True)
    session = _session(
        scripted,
        tmp_path,
        [calls(tool_call("run_command", command="echo hi")), answer("done")],
        approval_gate=gate,
    )
    session.run("go")
    assert gate.requests[0].summary == "echo hi"


# -- setup ---------------------------------------------------------------------


def test_an_explicitly_configured_model_is_used_without_probing(monkeypatch):
    probed: list[str] = []
    monkeypatch.setattr(
        "logic.session.discover_local_runtimes",
        lambda **kwargs: probed.append("probed") or (),
    )
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="http://configured/v1", model="chosen", provider="openai-compatible"
        )
    )
    setup = setup_from_config(config)
    assert setup.is_ready
    assert setup.inference.model == "chosen"
    assert probed == []


def test_discovery_fills_in_an_unconfigured_model(monkeypatch):
    runtime = DetectedRuntime(
        provider="ollama",
        base_url="http://localhost:11434/v1",
        reachable=True,
        models=(_GEMMA,),
    )
    monkeypatch.setattr("logic.session.discover_local_runtimes", lambda **kwargs: (runtime,))
    setup = setup_from_config(AutoSEConfig())
    assert setup.is_ready
    assert setup.inference.model == "gemma4:12b"
    assert setup.inference.context_limit == 262144


def test_no_runtime_produces_actionable_guidance(monkeypatch):
    monkeypatch.setattr("logic.session.discover_local_runtimes", lambda **kwargs: ())
    setup = setup_from_config(AutoSEConfig())
    assert not setup.is_ready
    assert "no local model runtime found" in setup.warnings[0]
    assert "ollama serve" in setup.warnings[0]


def test_a_runtime_serving_nothing_says_to_pull_a_model(monkeypatch):
    runtime = DetectedRuntime(
        provider="ollama", base_url="http://localhost:11434/v1", reachable=True
    )
    monkeypatch.setattr("logic.session.discover_local_runtimes", lambda **kwargs: (runtime,))
    setup = setup_from_config(AutoSEConfig())
    assert not setup.is_ready
    assert "ollama pull" in setup.warnings[0]


def test_a_model_without_tool_support_is_flagged_before_work_starts(monkeypatch):
    """DR-015: a pre-flight check, not a runtime surprise."""
    runtime = DetectedRuntime(
        provider="ollama", base_url="http://localhost:11434/v1", reachable=True, models=(_TINY,)
    )
    monkeypatch.setattr("logic.session.discover_local_runtimes", lambda **kwargs: (runtime,))
    setup = setup_from_config(AutoSEConfig())
    assert setup.is_ready
    assert any("no tool-calling support" in warning for warning in setup.warnings)


def test_selecting_another_model_in_setup_carries_its_context_limit(monkeypatch):
    runtime = DetectedRuntime(
        provider="ollama",
        base_url="http://localhost:11434/v1",
        reachable=True,
        models=(_GEMMA, _TINY),
    )
    monkeypatch.setattr("logic.session.discover_local_runtimes", lambda **kwargs: (runtime,))
    setup = setup_from_config(AutoSEConfig()).with_model("tiny:1b")
    assert setup.inference.context_limit == 4096
    assert any("tool-calling" in warning for warning in setup.warnings)


def test_discovery_can_be_disabled(monkeypatch):
    monkeypatch.setattr(
        "logic.session.discover_local_runtimes",
        lambda **kwargs: pytest.fail("discovery should not have run"),
    )
    setup = setup_from_config(AutoSEConfig(), discover=False)
    assert not setup.is_ready
    assert "discovery was disabled" in setup.warnings[0]


def test_auto_approval_from_config_requires_no_adapter_gate(monkeypatch, tmp_path, scripted):
    scripted["model"] = ScriptedChatModel(
        [calls(tool_call("run_command", command="echo hi")), answer("done")]
    )
    scripted["models"] = (_GEMMA,)
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_GEMMA.name, provider="test-session"
        ),
        workspace_root=tmp_path,
        auto_approve=True,
        auto_approve_authorized_by="ci",
    )
    monkeypatch.setattr("logic.session.discover_local_runtimes", lambda **kwargs: ())
    setup = setup_from_config(config)
    session = Session.from_setup(setup, auto_classify=False)
    turn = session.run("go")
    assert turn.answer == "done"


# -- DR-003: classification and the ratchet, through the facade ------------------


def _classifying(scripted, tmp_path, script, **kwargs):
    scripted["model"] = ScriptedChatModel(script)
    scripted["models"] = (_GEMMA,)
    return Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model=_GEMMA.name, provider="test-session"
        ),
        workspace=Workspace.at(tmp_path),
        model_info=_GEMMA,
        **kwargs,
    )


def test_a_prompt_with_no_explicit_tier_is_classified(scripted, tmp_path):
    session = _classifying(
        scripted, tmp_path, [answer('{"tier": "spark"}'), answer("the answer")]
    )
    turn = session.run("what does this do?")
    assert turn.tier == SPARK
    assert turn.classification is not None
    assert turn.classification.source == "model"


def test_a_classification_below_the_session_tier_is_ratcheted_up(scripted, tmp_path):
    """DR-003: a trivial-looking follow-up inside a complex task keeps the tier."""
    ledger = "\n\n## REQUIREMENTS STATUS\n- R1 [{status}] — Do the thing{evidence}"
    session = _classifying(
        scripted,
        tmp_path,
        [
            answer('{"tier": "spark"}'),
            answer("Plan." + ledger.format(status="Not Started", evidence="")),
            answer("Did it." + ledger.format(status="Implemented", evidence=" — Evidence: x.py")),
            answer(
                "Checked."
                + ledger.format(status="Verified", evidence=" — Evidence: read x.py")
                + "\n\nVERDICT: PASS"
            ),
        ],
    )
    session.request_tier(CRAFT)
    turn = session.run("yes, do that")
    assert turn.classification.tier == SPARK
    assert turn.tier == CRAFT
    assert session.tier == CRAFT


def test_an_explicit_tier_skips_classification(scripted, tmp_path):
    session = _classifying(scripted, tmp_path, [answer("done")])
    turn = session.run("go", tier=SPARK)
    assert turn.classification is None
    assert scripted["model"].call_count == 1


def test_classification_can_be_turned_off_entirely(scripted, tmp_path):
    session = _classifying(scripted, tmp_path, [answer("done")], auto_classify=False)
    turn = session.run("go")
    assert turn.classification is None
    assert turn.tier == SPARK


def test_classification_is_announced_as_events(scripted, tmp_path):
    events = RecordingEventSink()
    session = _classifying(
        scripted, tmp_path, [answer('{"tier": "spark"}'), answer("x")], events=events
    )
    session.run("go")
    assert events.of_type("classification_started")
    assert events.of_type("classification_finished")[0].data["tier"] == SPARK


def test_classifying_without_running_leaves_the_session_tier_alone(scripted, tmp_path):
    session = _classifying(scripted, tmp_path, [answer('{"tier": "craft"}')])
    assert session.classify("rewrite everything").tier == CRAFT
    assert session.tier == SPARK


def test_the_classification_event_describes_the_outcome_after_the_ratchet(scripted, tmp_path):
    """An Adapter should be able to render the tier before work starts, once."""
    events = RecordingEventSink()
    session = _classifying(
        scripted, tmp_path, [answer('{"tier": "spark"}'), answer("x")], events=events
    )
    session.run("go")
    data = events.of_type("classification_finished")[0].data
    assert data["tier"] == SPARK
    assert data["effective_tier"] == SPARK
    assert data["ratcheted"] is False


def test_a_ratcheted_classification_reports_both_tiers(scripted, tmp_path):
    events = RecordingEventSink()
    ledger = "\n\n## REQUIREMENTS STATUS\n- R1 [{status}] — Do it{evidence}"
    session = _classifying(
        scripted,
        tmp_path,
        [
            answer('{"tier": "spark"}'),
            answer("Plan." + ledger.format(status="Not Started", evidence="")),
            answer("Did." + ledger.format(status="Implemented", evidence=" — Evidence: x.py")),
            answer(
                "Checked."
                + ledger.format(status="Verified", evidence=" — Evidence: read x.py")
                + "\n\nVERDICT: PASS"
            ),
        ],
        events=events,
    )
    session.request_tier(CRAFT)
    session.run("yes, do that")
    data = events.of_type("classification_finished")[0].data
    assert data["tier"] == SPARK
    assert data["effective_tier"] == CRAFT
    assert data["ratcheted"] is True


# -- a window too small to work in ----------------------------------------------
#
# Ollama serves a 262k-capable model in 4,096 tokens by default. AutoSE has
# always detected that and warned; it then ran anyway, the budget evicted each
# round's file reads to fit, and the turn ended as "stalled" — which reads as
# the model's fault and sends the user to change the one thing that was fine.


_CAPPED = ModelInfo(
    name="gemma4:12b",
    context_length=4096,
    context_length_measured=True,
    max_context_length=262144,
    supports_tools=True,
)


def _capped_session(scripted, tmp_path, **kwargs) -> Session:
    scripted["model"] = ScriptedChatModel([answer("unreached")])
    scripted["models"] = (_CAPPED,)
    kwargs.setdefault("auto_classify", False)
    return Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model=_CAPPED.name,
            provider="test-session",
            context_limit=4096,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=_CAPPED,
        **kwargs,
    )


def test_a_turn_is_refused_outright_when_the_window_is_too_small(scripted, tmp_path):
    session = _capped_session(scripted, tmp_path)
    with pytest.raises(ConfigurationError) as caught:
        session.run("tell me about this project")
    message = str(caught.value)
    # The number, the remedy, and the way out — an error that only says "too
    # small" leaves the user exactly as stuck as the stall did.
    assert "4,096" in message and "8,192" in message
    assert "OLLAMA_CONTEXT_LENGTH" in message
    assert "min_usable_context_tokens: -1" in message


def test_the_session_stays_usable_so_the_model_can_be_changed(scripted, tmp_path):
    """Refusing at construction would take the model picker away along with
    the problem, leaving no way to fix it from inside the app."""
    session = _capped_session(scripted, tmp_path)
    assert session.available_models
    assert session.context_limit() == 4096
    session.select_model(_CAPPED.name)  # does not raise


def test_the_floor_can_be_waived_by_configuration(scripted, tmp_path):
    """A caller who has measured their own setup and disagrees is not stuck."""
    tuning = Tuning()
    tuning = replace(
        tuning, context=replace(tuning.context, min_usable_context_tokens=UNLIMITED)
    )
    session = _capped_session(scripted, tmp_path, tuning=tuning)
    assert session.run("hello").answer == "unreached"


def test_an_assumed_small_window_warns_but_does_not_refuse(scripted, tmp_path):
    """Ollama publishes the served window only for a *loaded* model.

    A cold server started with a raised `OLLAMA_CONTEXT_LENGTH` still looks
    like the 4096 default from outside, and refusing there would lock the user
    out of a server that was configured correctly all along.
    """
    assumed = replace(_CAPPED, context_length_measured=False)
    scripted["model"] = ScriptedChatModel([answer("ran anyway")])
    scripted["models"] = (assumed,)
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model=assumed.name,
            provider="test-session",
            context_limit=4096,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=assumed,
        auto_classify=False,
    )
    assert session.run("hello").answer == "ran anyway"
