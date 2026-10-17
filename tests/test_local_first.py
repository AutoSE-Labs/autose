"""DR-015: local runtimes are the default, discovered rather than demanded."""

from __future__ import annotations

import io
import json

import pytest

from logic.common.context import (
    DEFAULT_RESERVED_OUTPUT_TOKENS,
    MIN_RESERVED_OUTPUT_TOKENS,
    ContextBudget,
    reserved_output_for,
)
from logic.config import AutoSEConfig
from logic.inference import (
    DEFAULT_BASE_URL,
    DEFAULT_PROVIDER,
    InferenceSettings,
    ModelInfo,
    Provider,
    available_providers,
    get_provider,
    local_providers,
    register_provider,
)
from logic.inference.catalog import OpenAICompatibleCatalog
from logic.inference.discovery import discover_local_runtimes, probe_runtime
from logic.inference.ollama import (
    OLLAMA_CONTEXT_ENV,
    OLLAMA_DEFAULT_SERVED_CONTEXT,
    OllamaCatalog,
    native_root,
)
from logic.session import setup_from_config

# A trimmed copy of a real `ollama /api/tags` response.
_OLLAMA_TAGS = {
    "models": [
        {
            "name": "gemma4:12b",
            "model": "gemma4:12b",
            "size": 7556508396,
            "details": {
                "family": "gemma4",
                "parameter_size": "11.9B",
                "quantization_level": "Q4_K_M",
                "context_length": 262144,
            },
            "capabilities": ["completion", "tools", "thinking", "vision"],
        },
        {
            "name": "tiny:1b",
            "details": {"parameter_size": "1B", "context_length": 4096},
            "capabilities": ["completion"],
        },
    ]
}


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _serving(payload: dict, *, only_url: str | None = None):
    def fake_urlopen(request, timeout=None):
        if only_url is not None and request.full_url != only_url:
            raise OSError("connection refused")
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return fake_urlopen


def _serving_routes(routes: dict[str, dict]):
    """Serve a different payload per URL, refusing anything unrouted.

    DR-022 needs `/api/tags` and `/api/ps` to answer differently — the whole
    point is that they report different numbers — so a single-payload fake
    cannot express the case.
    """

    def fake_urlopen(request, timeout=None):
        payload = routes.get(request.full_url)
        if payload is None:
            raise OSError("connection refused")
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return fake_urlopen


#: A real `ollama /api/ps` response: gemma loaded in a 4096-token window,
#: despite `/api/tags` advertising a 262144-token maximum.
_OLLAMA_PS = {
    "models": [
        {
            "name": "gemma4:12b",
            "model": "gemma4:12b",
            "size_vram": 4829961255,
            "context_length": 4096,
        }
    ]
}


# -- defaults --------------------------------------------------------------


def test_the_default_provider_and_endpoint_are_local():
    assert DEFAULT_PROVIDER == "ollama"
    assert "localhost" in DEFAULT_BASE_URL
    assert get_provider(DEFAULT_PROVIDER).is_local


def test_a_complete_configuration_exists_with_nothing_set():
    settings = InferenceSettings()
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.api_key == ""
    assert settings.provider == "ollama"


def test_the_inference_timeout_suits_slow_local_hardware():
    assert (InferenceSettings().timeout or 0) >= 300


def test_discovery_timeouts_stay_short_enough_not_to_stall_startup():
    assert InferenceSettings().discovery_timeout <= 5


def test_local_providers_are_registered_and_marked_local():
    names = {provider.name for provider in local_providers()}
    assert {"ollama", "lm-studio", "vllm", "llama-cpp"} <= names
    assert all(provider.default_base_url for provider in local_providers())


def test_hosted_providers_remain_available_but_are_not_marked_local():
    assert not available_providers()["openai-compatible"].is_local


# -- the Ollama catalog -----------------------------------------------------


def test_native_root_strips_the_openai_compatible_suffix():
    assert native_root("http://localhost:11434/v1") == "http://localhost:11434"
    assert native_root("http://proxy/ollama") == "http://proxy/ollama"


def test_the_ollama_catalog_reads_capabilities_and_the_model_maximum(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _serving(_OLLAMA_TAGS))
    models = OllamaCatalog(InferenceSettings()).list_models()

    gemma = next(model for model in models if model.name == "gemma4:12b")
    assert gemma.max_context_length == 262144
    assert gemma.supports_tools is True
    assert gemma.parameter_size == "11.9B"
    assert gemma.quantization == "Q4_K_M"


def test_the_budget_uses_the_served_window_not_the_model_maximum(monkeypatch):
    """DR-022: `/api/ps` reports what is actually served; that is the budget."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving_routes(
            {
                "http://localhost:11434/api/tags": _OLLAMA_TAGS,
                "http://localhost:11434/api/ps": _OLLAMA_PS,
            }
        ),
    )
    gemma = next(
        model
        for model in OllamaCatalog(InferenceSettings()).list_models()
        if model.name == "gemma4:12b"
    )
    assert gemma.context_length == 4096, "must budget against the served window"
    assert gemma.max_context_length == 262144
    assert gemma.is_context_capped


def test_an_unloaded_model_falls_back_to_ollamas_own_default(monkeypatch):
    """DR-022: with nothing loaded the served window is unknowable.

    Assuming Ollama's documented default is conservative and self-correcting —
    never the model maximum, which is what silently overflowed every request.
    """
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving_routes(
            {
                "http://localhost:11434/api/tags": _OLLAMA_TAGS,
                "http://localhost:11434/api/ps": {"models": []},
            }
        ),
    )
    models = OllamaCatalog(InferenceSettings()).list_models()

    gemma = next(model for model in models if model.name == "gemma4:12b")
    assert gemma.context_length == OLLAMA_DEFAULT_SERVED_CONTEXT
    assert gemma.max_context_length == 262144
    assert gemma.is_context_capped

    # A model whose own maximum is below the default is not overstated up to it.
    tiny = next(model for model in models if model.name == "tiny:1b")
    assert tiny.context_length == 4096
    assert not tiny.is_context_capped


def test_a_capped_window_warns_with_the_command_that_raises_it(monkeypatch):
    """DR-022: the warning has to be actionable, not merely accurate."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving_routes(
            {
                "http://localhost:11434/api/tags": _OLLAMA_TAGS,
                "http://localhost:11434/api/ps": _OLLAMA_PS,
            }
        ),
    )
    setup = setup_from_config(AutoSEConfig())
    joined = " ".join(setup.warnings)
    assert OLLAMA_CONTEXT_ENV in joined
    assert "4,096" in joined and "262,144" in joined


def test_a_model_without_the_tools_capability_is_reported_as_unable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _serving(_OLLAMA_TAGS))
    models = OllamaCatalog(InferenceSettings()).list_models()
    tiny = next(model for model in models if model.name == "tiny:1b")
    assert tiny.supports_tools is False
    assert not tiny.is_tool_capable


def test_an_unstated_capability_list_is_unknown_not_false(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", _serving({"models": [{"name": "old:7b"}]})
    )
    model = OllamaCatalog(InferenceSettings()).list_models()[0]
    assert model.supports_tools is None
    assert not model.is_tool_capable


def test_the_openai_compatible_catalog_returns_bare_names(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving({"data": [{"id": "b-model"}, {"id": "a-model"}]}),
    )
    models = OpenAICompatibleCatalog(InferenceSettings()).list_models()
    assert [model.name for model in models] == ["a-model", "b-model"]
    assert models[0].context_length is None


def test_a_model_description_is_readable():
    described = ModelInfo(
        name="gemma4:12b",
        context_length=262144,
        parameter_size="11.9B",
        quantization="Q4_K_M",
        supports_tools=True,
    ).describe()
    assert "gemma4:12b" in described and "256k ctx" in described

    warned = ModelInfo(name="tiny:1b", supports_tools=False).describe()
    assert "no tool calling" in warned


# -- probing ----------------------------------------------------------------


def test_probing_a_live_runtime_reports_its_models(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _serving(_OLLAMA_TAGS))
    detected = probe_runtime(get_provider("ollama"))
    assert detected.reachable and detected.usable
    assert len(detected.models) == 2


def test_probing_a_dead_endpoint_reports_absence_rather_than_raising(monkeypatch):
    def refuse(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    detected = probe_runtime(get_provider("ollama"))
    assert not detected.reachable
    assert "connection refused" in detected.error
    assert "not reachable" in detected.summary()


def test_an_unexpected_probe_failure_cannot_break_startup(monkeypatch):
    def explode(request, timeout=None):
        raise RuntimeError("something bizarre")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    detected = probe_runtime(get_provider("ollama"))
    assert not detected.reachable
    assert "unexpected probe failure" in detected.error


def test_a_provider_with_no_conventional_address_is_skipped_cleanly():
    detected = probe_runtime(get_provider("openai-compatible"))
    assert not detected.reachable
    assert "configure base_url" in detected.error


def test_a_reachable_runtime_serving_nothing_is_distinguished(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _serving({"models": []}))
    detected = probe_runtime(get_provider("ollama"))
    assert detected.reachable and not detected.usable
    assert "no models available" in detected.summary()


def test_discovery_probes_every_local_provider_and_ranks_usable_first(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving(_OLLAMA_TAGS, only_url="http://localhost:11434/api/tags"),
    )
    found = discover_local_runtimes(include_unreachable=True)
    assert len(found) == len(local_providers())
    assert found[0].provider == "ollama"
    assert found[0].usable
    assert not any(runtime.usable for runtime in found[1:])


def test_discovery_hides_unreachable_runtimes_by_default(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving(_OLLAMA_TAGS, only_url="http://localhost:11434/api/tags"),
    )
    assert [runtime.provider for runtime in discover_local_runtimes()] == ["ollama"]


def test_settings_built_from_discovery_carry_the_published_context_limit(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _serving(_OLLAMA_TAGS))
    detected = probe_runtime(get_provider("ollama"))

    default = detected.settings()
    assert default.model == "gemma4:12b"
    # DR-022: the served window, not the 262144-token model maximum.
    assert default.context_limit == OLLAMA_DEFAULT_SERVED_CONTEXT
    assert default.provider == "ollama"

    small = detected.settings("tiny:1b")
    assert small.context_limit == 4096


def test_a_custom_local_runtime_plugs_in_without_editing_discovery(monkeypatch):
    register_provider(
        Provider(
            name="test-runtime",
            chat_model=lambda settings: None,
            is_local=True,
            default_base_url="http://localhost:9999/v1",
        ),
        replace=True,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving({"data": [{"id": "plugged-in"}]}, only_url="http://localhost:9999/v1/models"),
    )
    found = discover_local_runtimes(include_unreachable=True)
    assert any(runtime.provider == "test-runtime" and runtime.usable for runtime in found)


# -- output reservation scaled to the window --------------------------------


@pytest.mark.parametrize(
    ("context_limit", "expected"),
    [
        (2048, 512),
        (4096, 1024),
        (8192, 2048),
        (32_768, 8192),
        (262_144, 8192),
    ],
)
def test_reserved_output_scales_with_the_window(context_limit, expected):
    assert reserved_output_for(context_limit) == expected


def test_reserved_output_is_bounded_at_both_ends():
    assert reserved_output_for(64) == MIN_RESERVED_OUTPUT_TOKENS
    assert reserved_output_for(10_000_000) == DEFAULT_RESERVED_OUTPUT_TOKENS
    assert reserved_output_for(None) == DEFAULT_RESERVED_OUTPUT_TOKENS


def test_a_small_local_model_still_gets_a_usable_input_budget():
    """The bug this exists to prevent: a fixed 8k reservation eats a 4k window."""
    budget = ContextBudget(context_limit=4096)
    prepared = budget.prepare([{"role": "user", "content": "hello"}])
    assert prepared.metrics.message_budget > 1000


def test_an_explicit_zero_reservation_is_still_honoured():
    assert ContextBudget(context_limit=4096, reserved_output_tokens=0).reserved_output_tokens == 0


def test_the_doctor_advises_how_to_raise_a_capped_window(monkeypatch):
    """DR-022: `--doctor` is where a user goes to find out what they have."""
    from logic.diagnostics import diagnose

    monkeypatch.setattr(
        "urllib.request.urlopen",
        _serving_routes(
            {
                "http://localhost:11434/api/tags": _OLLAMA_TAGS,
                "http://localhost:11434/api/ps": _OLLAMA_PS,
            }
        ),
    )
    diagnosis = diagnose(AutoSEConfig())

    joined = " ".join(diagnosis.advisories)
    assert OLLAMA_CONTEXT_ENV in joined
    assert "4,096" in joined and "262,144" in joined
    # A capped window is a constraint, not a fault: the setup still works.
    assert diagnosis.healthy
