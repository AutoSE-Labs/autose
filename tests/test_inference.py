"""DR-006: one OpenAI-compatible shape, provider quirks handled at the edge."""

from __future__ import annotations

import io
import json
import re
import urllib.error

import pytest

from logic.errors import (
    ConfigurationError,
    InferenceError,
    InferenceTimeoutError,
    ProviderContextLengthError,
)
from logic.inference import (
    DEFAULT_PROVIDER,
    InferenceSettings,
    Provider,
    available_providers,
    create_chat_model,
    register_provider,
)
from logic.inference.openai_compatible import OpenAICompatibleChatModel
from logic.inference.types import CompletionResult, ToolCall, Usage


def _settings(**overrides) -> InferenceSettings:
    base = {"base_url": "http://backend.test/v1/", "model": "test-model"}
    base.update(overrides)
    return InferenceSettings(**base)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _json_response(payload: dict) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def _sse_response(lines: list[str]) -> _FakeResponse:
    return _FakeResponse("".join(f"{line}\n" for line in lines).encode("utf-8"))


# -- settings ------------------------------------------------------------


def test_settings_normalise_the_base_url():
    assert _settings().base_url == "http://backend.test/v1"


def test_settings_require_a_base_url():
    with pytest.raises(ConfigurationError):
        InferenceSettings(base_url="", model="m")


def test_an_unset_model_is_a_pre_selection_state_not_an_error():
    """DR-015: discovery fills the model in, so it starts empty."""
    settings = InferenceSettings(base_url="http://x/v1")
    assert settings.model == ""
    assert not settings.is_ready
    with pytest.raises(ConfigurationError, match="no model selected"):
        settings.require_model()
    assert settings.with_model("m").require_model().model == "m"


def test_an_api_key_is_optional_for_local_backends():
    assert _settings().api_key == ""


# -- registry ------------------------------------------------------------


def test_the_default_provider_is_registered():
    assert DEFAULT_PROVIDER in available_providers()
    assert isinstance(create_chat_model(_settings()), OpenAICompatibleChatModel)


def test_an_unknown_provider_is_a_configuration_error():
    with pytest.raises(ConfigurationError, match="unknown inference provider"):
        create_chat_model(_settings(provider="not-real"))


def test_a_second_implementation_plugs_in_without_editing_the_selector():
    class _Custom:
        def __init__(self, settings):
            self.model = settings.model

        def complete(self, messages, *, tools=None, max_output_tokens=None):
            return CompletionResult(content="custom")

        def stream(self, messages, *, tools=None, max_output_tokens=None):
            yield from ()

    register_provider(
        Provider(name="test-custom", chat_model=_Custom), replace=True
    )
    model = create_chat_model(_settings(provider="test-custom"))
    assert model.complete([]).content == "custom"


def test_registering_a_name_twice_is_refused():
    provider = Provider(name="test-dup", chat_model=lambda settings: None)
    register_provider(provider, replace=True)
    with pytest.raises(ConfigurationError, match="already registered"):
        register_provider(provider)


# -- request shape --------------------------------------------------------


def test_the_request_targets_chat_completions_with_the_configured_model(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data)
        return _json_response(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    model = OpenAICompatibleChatModel(_settings(api_key="secret", temperature=0.7))
    model.complete([{"role": "user", "content": "hi"}], max_output_tokens=64)

    assert captured["url"] == "http://backend.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["temperature"] == 0.7
    assert captured["body"]["max_tokens"] == 64
    assert "stream" not in captured["body"]


def test_no_authorization_header_is_sent_without_an_api_key(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        return _json_response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    OpenAICompatibleChatModel(_settings()).complete([])
    assert "Authorization" not in captured["headers"]


# -- response normalisation ------------------------------------------------


def test_tool_calls_are_normalised_from_the_standard_string_form(monkeypatch):
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "m.py"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _json_response(payload)
    )
    result = OpenAICompatibleChatModel(_settings()).complete([])
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].arguments == {"path": "m.py"}
    assert result.usage.total_tokens == 14


def test_a_provider_sending_arguments_as_an_object_is_accepted():
    call = ToolCall.from_payload(
        {"id": "c", "function": {"name": "read_file", "arguments": {"path": "m.py"}}}
    )
    assert call.arguments == {"path": "m.py"}
    assert not call.parse_error


def test_a_provider_omitting_the_call_id_gets_a_synthetic_one():
    call = ToolCall.from_payload(
        {"function": {"name": "read_file", "arguments": "{}"}}, fallback_index=2
    )
    assert call.id == "call_2"


def test_unparsable_arguments_are_flagged_not_raised():
    call = ToolCall.from_payload({"function": {"name": "x", "arguments": "{oops"}})
    assert call.parse_error
    assert call.arguments == {}


def test_non_object_arguments_are_flagged():
    call = ToolCall.from_payload({"function": {"name": "x", "arguments": "[1,2]"}})
    assert "must be a JSON object" in call.parse_error


def test_a_response_with_no_choices_is_an_inference_error(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _json_response({})
    )
    with pytest.raises(InferenceError, match="no choices"):
        OpenAICompatibleChatModel(_settings()).complete([])


def test_a_non_json_response_is_an_inference_error(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(b"<html>gateway</html>"),
    )
    with pytest.raises(InferenceError, match="non-JSON"):
        OpenAICompatibleChatModel(_settings()).complete([])


def test_the_assistant_message_is_rebuilt_from_normalised_fields():
    result = CompletionResult(
        content="",
        tool_calls=(
            ToolCall(id="c1", name="read_file", raw_arguments='{"path":"m.py"}'),
        ),
    )
    message = result.to_message()
    assert message["role"] == "assistant"
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "read_file"


# -- error translation ------------------------------------------------------


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://backend.test/v1/chat/completions",
        code,
        "err",
        {},
        io.BytesIO(body.encode("utf-8")),
    )


def test_a_context_window_rejection_becomes_its_own_typed_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise _http_error(400, "This model's maximum context length is 8192 tokens")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ProviderContextLengthError):
        OpenAICompatibleChatModel(_settings()).complete([])


def test_other_http_errors_stay_generic_inference_errors(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise _http_error(503, "upstream unavailable")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(InferenceError) as excinfo:
        OpenAICompatibleChatModel(_settings()).complete([])
    assert not isinstance(excinfo.value, ProviderContextLengthError)


def test_an_unreachable_backend_is_a_timeout_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(InferenceTimeoutError, match=re.escape("backend.test")):
        OpenAICompatibleChatModel(_settings()).complete([])


# -- streaming ---------------------------------------------------------------


def test_streaming_yields_content_then_a_final_assembled_result(monkeypatch):
    """DR-014: the final chunk carries what complete() would have returned."""
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _sse_response(lines)
    )
    chunks = list(OpenAICompatibleChatModel(_settings()).stream([]))
    assert [chunk.content for chunk in chunks[:-1]] == ["Hel", "lo"]
    assert chunks[-1].is_final
    assert chunks[-1].result.content == "Hello"
    assert chunks[-1].result.finish_reason == "stop"


def test_streamed_tool_call_arguments_are_stitched_back_together(monkeypatch):
    """DR-014: no second generation is needed to recover a round's tool calls."""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"read_file","arguments":"{\\"pa"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"th\\": \\"m.py\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _sse_response(lines)
    )
    final = list(OpenAICompatibleChatModel(_settings()).stream([]))[-1].result
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].name == "read_file"
    assert final.tool_calls[0].arguments == {"path": "m.py"}


def test_a_backend_without_sse_support_falls_back_to_one_plain_call(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data)
        if body.get("stream"):
            calls.append("stream")
            raise _http_error(404, "streaming not supported")
        calls.append("sync")
        return _json_response(
            {"choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chunks = list(OpenAICompatibleChatModel(_settings()).stream([]))
    assert calls == ["stream", "sync"]
    assert chunks[-1].result.content == "fallback"


def test_malformed_sse_lines_are_skipped(monkeypatch):
    lines = [
        ": keep-alive comment",
        "data: not-json",
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _sse_response(lines)
    )
    assert list(OpenAICompatibleChatModel(_settings()).stream([]))[-1].result.content == "ok"


def test_usage_defaults_to_zero_when_a_provider_omits_it():
    assert Usage.from_payload(None).total_tokens == 0


# -- non-streaming rounds and request extras ------------------------------


def test_stream_false_makes_one_plain_request_and_yields_the_same_chunks(monkeypatch):
    bodies: list[dict] = []

    def fake_urlopen(request, timeout=None):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _json_response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "answer", "reasoning": "thought"},
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    model = OpenAICompatibleChatModel(_settings(stream=False))
    chunks = list(model.stream([{"role": "user", "content": "hi"}]))

    assert len(bodies) == 1 and "stream" not in bodies[0]
    assert [c.reasoning for c in chunks if c.reasoning] == ["thought"]
    assert [c.content for c in chunks if c.content] == ["answer"]
    assert chunks[-1].result.usage.completion_tokens == 3


def test_request_extras_reach_the_body_but_cannot_replace_core_fields(monkeypatch):
    bodies: list[dict] = []

    def fake_urlopen(request, timeout=None):
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _json_response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    extras = {
        "top_k": 20,
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0.7,
        "model": "other-model",
    }
    OpenAICompatibleChatModel(_settings(request_extras=extras)).complete(
        [{"role": "user", "content": "hi"}]
    )

    body = bodies[0]
    assert body["top_k"] == 20
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["temperature"] == 0.7
    assert body["model"] == "test-model"


def test_a_gateway_error_is_retried_before_failing(monkeypatch):
    attempts: list[int] = []

    def fake_urlopen(request, timeout=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                "http://backend.test/v1/chat/completions", 502, "Bad Gateway", {}, io.BytesIO(b"")
            )
        return _json_response({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = OpenAICompatibleChatModel(_settings()).complete([{"role": "user", "content": "hi"}])
    assert result.content == "ok"
    assert len(attempts) == 2
