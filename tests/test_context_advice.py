"""DR-034: one verdict about a served context window, with a computed remedy."""

from __future__ import annotations

import io
import json

from logic.common.context import MIN_USABLE_CONTEXT_TOKENS
from logic.inference.catalog import ModelInfo
from logic.inference.context_advice import advise_context, context_remedy
from logic.inference.ollama import OllamaCatalog
from logic.inference.settings import InferenceSettings


def _model(served, maximum=262_144, *, gpu=None, measured=True) -> ModelInfo:
    return ModelInfo(
        name="qwen3.5:9b",
        context_length=served,
        context_length_measured=measured,
        max_context_length=maximum,
        gpu_fraction=gpu,
    )


def _advise(info: ModelInfo, provider: str = "ollama"):
    return advise_context(info, provider=provider, min_usable=MIN_USABLE_CONTEXT_TOKENS)


def test_a_deliberate_cap_on_a_model_that_spills_to_cpu_is_not_a_warning():
    """The case that prompted DR-034: 32k of 262k, 42% on the CPU.

    That is the right setting for the machine. Raising it would make the model
    slower, so the advice is to keep it — and it must not offer a command.
    """
    advice = _advise(_model(32_768, gpu=0.58))
    assert advice.level == "info"
    assert not advice.needs_attention
    assert advice.recommended is None
    assert advice.remedy == ""
    assert "42%" in advice.detail


def test_a_cap_on_a_model_that_fits_the_gpu_offers_more_room_without_alarm():
    advice = _advise(_model(32_768, gpu=1.0))
    assert advice.level == "info"
    assert advice.recommended == 65_536
    assert advice.remedy == "OLLAMA_CONTEXT_LENGTH=65536 ollama serve"


def test_a_small_but_usable_window_is_a_warning_with_a_computed_target():
    advice = _advise(_model(16_384))
    assert advice.level == "warning"
    assert advice.needs_attention
    assert advice.recommended == 32_768
    assert "32768" in advice.remedy


def test_a_window_below_the_floor_blocks_and_never_suggests_the_value_in_use():
    advice = _advise(_model(4_096))
    assert advice.level == "blocking"
    assert advice.recommended == 32_768
    assert "4096" not in advice.remedy


def test_the_target_never_exceeds_what_the_model_supports():
    advice = _advise(_model(4_096, maximum=16_384))
    assert advice.recommended == 16_384


def test_the_full_window_needs_no_advice():
    advice = _advise(_model(262_144))
    assert advice.level == "ok"
    assert advice.remedy == ""


def test_an_unknown_runtime_gets_a_target_but_no_borrowed_command():
    advice = _advise(_model(16_384), provider="openai-compatible")
    assert advice.recommended == 32_768
    assert advice.remedy == ""


def test_an_assumed_window_says_so():
    advice = _advise(_model(4_096, measured=False))
    assert "assumed" in advice.summary


def test_each_runtime_gets_its_own_command():
    assert context_remedy("vllm", 32_768).endswith("--max-model-len 32768")
    assert context_remedy("llama-cpp", 32_768).endswith("-c 32768")
    assert context_remedy("nobody", 32_768) == ""


def test_ollama_reports_how_much_of_a_loaded_model_is_on_the_gpu(monkeypatch):
    routes = {
        "http://localhost:11434/api/tags": {
            "models": [
                {
                    "name": "qwen3.5:9b",
                    "details": {"context_length": 262_144},
                    "capabilities": ["tools"],
                }
            ]
        },
        "http://localhost:11434/api/ps": {
            "models": [
                {
                    "name": "qwen3.5:9b",
                    "size": 6_918_347_157,
                    "size_vram": 4_045_951_465,
                    "context_length": 32_768,
                }
            ]
        },
    }

    def fake_urlopen(request, timeout=None):
        url = getattr(request, "full_url", request)
        return io.BytesIO(json.dumps(routes[url]).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    (model,) = OllamaCatalog(InferenceSettings()).list_models()
    assert model.context_length == 32_768
    assert model.gpu_fraction is not None
    assert round(model.gpu_fraction, 2) == 0.58


def test_an_unknown_gpu_share_is_not_reported_as_fitting():
    """Right after the window changes, the GPU share is unknown — not "fits"."""
    advice = _advise(_model(65_536, gpu=None))
    assert advice.level == "info"
    assert "on the GPU" in advice.detail
    assert "whole model" not in advice.detail
    assert advice.recommended is None


def test_where_autose_chooses_the_window_the_remedy_keeps_the_current_one():
    advice = advise_context(
        _model(65_536, gpu=None),
        provider="ollama",
        min_usable=MIN_USABLE_CONTEXT_TOKENS,
        adjustable=True,
    )
    assert advice.remedy == "inference:\n  context_limit: 65536"

