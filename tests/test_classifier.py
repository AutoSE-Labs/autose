"""DR-003: one classification call, deterministic fallback, never crashing."""

from __future__ import annotations

from logic.classifier import TaskClassifier
from logic.errors import InferenceTimeoutError
from logic.tiers import CRAFT, DEFAULT_TIER, SPARK
from logic.tuning import ClassifierTuning
from tests.support.fakes import ScriptedChatModel, answer


def _classify(text: str, **kwargs):
    return TaskClassifier(ScriptedChatModel([answer(text)]), **kwargs).classify(
        "do a thing"
    )


def test_a_clean_json_answer_is_used():
    result = _classify('{"tier": "craft"}')
    assert result.tier == CRAFT
    assert result.source == "model"
    assert result.is_confident


def test_json_inside_a_code_fence_is_read():
    assert _classify('```json\n{"tier": "spark"}\n```').tier == SPARK


def test_json_surrounded_by_prose_is_read():
    result = _classify('Sure! Here you go: {"tier": "craft"} — hope that helps.')
    assert result.tier == CRAFT
    assert result.source == "model"


def test_alternative_key_names_are_accepted():
    assert _classify('{"mode": "craft"}').tier == CRAFT
    assert _classify('{"level": "spark"}').tier == SPARK


def test_prose_naming_a_tier_falls_back_to_a_text_scan():
    result = _classify("I would call this a craft task.")
    assert result.tier == CRAFT
    assert result.source == "text_scan"
    assert not result.is_confident


def test_the_text_scan_reads_intent_when_no_tier_is_named():
    assert _classify("This needs an architectural redesign.").tier == CRAFT
    assert _classify("Just implement the feature.").tier == CRAFT


def test_the_pro_only_forge_answer_lands_on_craft():
    """A model that still answers `forge` (DR-032) gets the tier that exists."""
    assert _classify('{"tier": "forge"}').tier == CRAFT
    assert _classify("This is forge-level work.").tier == CRAFT


def test_an_unparsable_answer_lands_on_the_middle_tier():
    """DR-003: guessing high is expensive, guessing low under-powers."""
    result = _classify("purple monkey dishwasher")
    assert result.tier == DEFAULT_TIER == CRAFT
    assert result.source == "fallback"


def test_an_empty_answer_lands_on_the_middle_tier():
    assert _classify("").tier == DEFAULT_TIER


def test_an_unknown_tier_name_is_not_accepted():
    assert _classify('{"tier": "advanced"}').tier == DEFAULT_TIER


def test_an_empty_prompt_is_not_sent_to_the_model():
    model = ScriptedChatModel([])
    result = TaskClassifier(model).classify("   ")
    assert result.tier == DEFAULT_TIER
    assert model.call_count == 0


def test_an_unreachable_backend_does_not_crash_the_run():
    class _Broken:
        model = "m"

        def complete(self, messages, **kwargs):
            raise InferenceTimeoutError("backend down")

        def stream(self, messages, **kwargs):
            raise InferenceTimeoutError("backend down")

    result = TaskClassifier(_Broken()).classify("do a thing")
    assert result.tier == DEFAULT_TIER
    assert "backend down" in result.detail


def test_an_unexpected_error_does_not_crash_the_run():
    class _Exploding:
        model = "m"

        def complete(self, messages, **kwargs):
            raise RuntimeError("something bizarre")

        def stream(self, messages, **kwargs):
            raise RuntimeError("something bizarre")

    result = TaskClassifier(_Exploding()).classify("do a thing")
    assert result.tier == DEFAULT_TIER
    assert "unexpected" in result.detail


def test_a_bad_placeholder_in_a_custom_prompt_falls_back_rather_than_crashing():
    """DR-021 invites editing these prompts; a stray '{' must not crash a run."""
    classifier = TaskClassifier(
        ScriptedChatModel([answer('{"tier": "craft"}')]),
        prompt_overrides={"classifier_user": "Classify this {thing}: {prompt}"},
    )
    result = classifier.classify("do a thing")
    assert result.tier == DEFAULT_TIER
    assert result.source == "fallback"
    assert "thing" in result.detail


def test_classification_costs_exactly_one_call():
    model = ScriptedChatModel([answer('{"tier": "craft"}')])
    TaskClassifier(model).classify("do a thing")
    assert model.call_count == 1


def test_the_call_is_capped_so_it_stays_cheap():
    model = ScriptedChatModel([answer('{"tier": "craft"}')])
    TaskClassifier(model).classify("do a thing")
    assert model.requests[0]["max_output_tokens"] <= 500


def test_recent_history_is_offered_as_read_only_context():
    model = ScriptedChatModel([answer('{"tier": "craft"}')])
    history = [{"role": "user", "content": f"turn {i}"} for i in range(10)]
    tuning = ClassifierTuning(max_history_messages=3)
    TaskClassifier(model, tuning=tuning).classify("yes, do that", history=history)
    contents = [m["content"] for m in model.requests[0]["messages"]]
    assert "turn 9" in contents
    assert "turn 0" not in contents


def test_the_prompt_lists_every_tier():
    model = ScriptedChatModel([answer('{"tier": "craft"}')])
    prompt = TaskClassifier(model)._system_prompt()
    for tier in (SPARK, CRAFT):
        assert tier in prompt
    assert "forge" not in prompt
