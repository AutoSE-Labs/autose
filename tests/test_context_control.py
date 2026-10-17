"""DR-035: AutoSE chooses the window Ollama serves, and the user can change it."""

from __future__ import annotations

import io
import json

import pytest

from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.errors import ConfigurationError, ProviderContextLengthError
from logic.inference.catalog import ModelInfo
from logic.inference.context_advice import advise_context, window_choices
from logic.inference.ollama_chat import OllamaChatModel, to_native_messages
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.session import Session, adopt_context_window
from tests.support.fakes import ScriptedChatModel, answer


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _capturing(lines: list[dict], sent: list):
    """A fake ``urlopen`` that records each request and answers with *lines*."""

    def fake_urlopen(request, timeout=None):
        sent.append(request)
        body = "\n".join(json.dumps(line) for line in lines).encode("utf-8")
        return _Response(body)

    return fake_urlopen


def _settings(**overrides) -> InferenceSettings:
    values = {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3.5:9b",
        "provider": "ollama",
        "context_limit": 32768,
    }
    values.update(overrides)
    return InferenceSettings(**values)


# -- the native client -----------------------------------------------------------


def test_every_request_asks_for_the_window_autose_budgets_against(monkeypatch):
    """The whole point of DR-035: `num_ctx` reaches the runtime."""
    sent: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _capturing([{"message": {"content": "ok"}, "done": True}], sent),
    )
    OllamaChatModel(_settings(context_limit=65536)).complete(
        [{"role": "user", "content": "hi"}], max_output_tokens=512
    )
    (request,) = sent
    assert request.full_url == "http://localhost:11434/api/chat"
    body = json.loads(request.data)
    assert body["options"]["num_ctx"] == 65536
    assert body["options"]["num_predict"] == 512
    assert body["stream"] is False


def test_a_native_reply_is_normalised_like_any_other(monkeypatch):
    sent: list = []
    reply = {
        "message": {
            "content": "",
            "thinking": "look at the file first",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
            ],
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1200,
        "eval_count": 40,
    }
    monkeypatch.setattr("urllib.request.urlopen", _capturing([reply], sent))
    result = OllamaChatModel(_settings()).complete([{"role": "user", "content": "x"}])
    assert result.finish_reason == "tool_calls"
    (call,) = result.tool_calls
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.py"}
    assert result.usage.prompt_tokens == 1200
    assert result.usage.completion_tokens == 40
    assert result.reasoning == "look at the file first"


def test_a_streamed_reply_is_reassembled_from_ndjson(monkeypatch):
    sent: list = []
    lines = [
        {"message": {"thinking": "hm"}, "done": False},
        {"message": {"content": "Hel"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {}, "done": True, "prompt_eval_count": 9, "eval_count": 2},
    ]
    monkeypatch.setattr("urllib.request.urlopen", _capturing(lines, sent))
    chunks = list(
        OllamaChatModel(_settings()).stream([{"role": "user", "content": "x"}])
    )
    assert "".join(chunk.content for chunk in chunks) == "Hello"
    assert "".join(chunk.reasoning for chunk in chunks) == "hm"
    final = chunks[-1].result
    assert final is not None and final.content == "Hello"
    assert final.usage.completion_tokens == 2
    assert json.loads(sent[0].data)["stream"] is True


def test_an_error_line_about_the_window_is_a_typed_context_error(monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _capturing([{"error": "input exceeds the context length (num_ctx)"}], sent),
    )
    with pytest.raises(ProviderContextLengthError):
        list(OllamaChatModel(_settings()).stream([{"role": "user", "content": "x"}]))


def test_asking_for_no_reasoning_turns_thinking_off(monkeypatch):
    sent: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _capturing([{"message": {"content": "spark"}, "done": True}], sent),
    )
    OllamaChatModel(_settings()).complete(
        [{"role": "user", "content": "x"}], reasoning_effort="none"
    )
    assert json.loads(sent[0].data)["think"] is False


def test_history_is_translated_to_the_native_shape():
    """Native tool calls take an object, and a result names its tool."""
    native = to_native_messages(
        [
            {"role": "system", "content": "be brief"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_7",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_7", "content": "print(1)"},
        ]
    )
    assert native[0] == {"role": "system", "content": "be brief"}
    assert native[1]["tool_calls"] == [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
    ]
    assert native[1]["content"] == ""
    assert native[2] == {"role": "tool", "content": "print(1)", "tool_name": "read_file"}


# -- choosing the window -----------------------------------------------------------


_COLD = ModelInfo(name="qwen3.5:9b", context_length=4096, max_context_length=262144)


def test_an_unloaded_ollama_model_gets_a_comfortable_window_not_the_default():
    inference, info = adopt_context_window(_settings(context_limit=None), _COLD, explicit=None)
    assert inference.context_limit == 32768
    assert info is not None
    assert info.context_length == 32768
    assert info.context_length_measured


def test_a_configured_window_wins_but_never_past_the_model():
    inference, _ = adopt_context_window(_settings(), _COLD, explicit=65536)
    assert inference.context_limit == 65536
    small = ModelInfo(name="tiny", context_length=4096, max_context_length=16384)
    inference, _ = adopt_context_window(_settings(), small, explicit=None)
    assert inference.context_limit == 16384


def test_a_gpu_share_measured_at_another_window_is_dropped():
    loaded = ModelInfo(
        name="qwen3.5:9b",
        context_length=32768,
        context_length_measured=True,
        max_context_length=262144,
        gpu_fraction=0.58,
    )
    _, same = adopt_context_window(_settings(), loaded, explicit=32768)
    assert same is not None and same.gpu_fraction == 0.58
    _, other = adopt_context_window(_settings(), loaded, explicit=65536)
    assert other is not None and other.gpu_fraction is None


def test_an_adjustable_runtime_is_advised_with_autose_s_own_setting():
    advice = advise_context(
        ModelInfo(name="m", context_length=16384, max_context_length=262144),
        provider="ollama",
        min_usable=8192,
        adjustable=True,
    )
    assert advice.adjustable
    assert advice.remedy == "inference:\n  context_limit: 32768"


def test_the_offered_windows_explain_themselves():
    info = ModelInfo(
        name="m",
        context_length=32768,
        context_length_measured=True,
        max_context_length=65536,
        gpu_fraction=0.58,
    )
    choices = window_choices(info, min_usable=8192)
    assert [choice.tokens for choice in choices] == [8192, 16384, 32768, 65536]
    current = next(choice for choice in choices if choice.current)
    assert current.tokens == 32768 and current.recommended
    assert "slower" in choices[-1].note


# -- changing it from a session ----------------------------------------------------


@pytest.fixture
def adjustable(monkeypatch):
    """A provider that, like Ollama, serves the window it is asked for."""
    holder: dict = {"built": []}

    def factory(settings):
        holder["built"].append(settings)
        return ScriptedChatModel([answer("ok")])

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return holder.get("models", ())

    register_provider(
        Provider(
            name="test-window",
            chat_model=factory,
            catalog=_Catalog,
            serves_requested_window=True,
        ),
        replace=True,
    )
    return holder


def _window_session(tmp_path, events=None) -> Session:
    return Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model="qwen3.5:9b",
            provider="test-window",
            context_limit=32768,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=ModelInfo(
            name="qwen3.5:9b", context_length=32768, max_context_length=131072
        ),
        events=events,
        auto_classify=False,
    )


def test_a_session_serves_a_chosen_window_from_the_next_request(adjustable, tmp_path):
    events = RecordingEventSink()
    session = _window_session(tmp_path, events)
    session.set_context_window(65536)
    assert session.context_limit() == 65536
    assert adjustable["built"][-1].context_limit == 65536
    (changed,) = events.of_type("context_window_changed")
    assert changed.data["tokens"] == 65536


def test_a_window_outside_what_works_is_refused(adjustable, tmp_path):
    session = _window_session(tmp_path)
    with pytest.raises(ConfigurationError):
        session.set_context_window(4096)
    with pytest.raises(ConfigurationError):
        session.set_context_window(262144)


def test_a_runtime_that_cannot_honour_a_window_refuses_to_pretend(tmp_path, monkeypatch):
    register_provider(
        Provider(
            name="test-fixed-window",
            chat_model=lambda settings: ScriptedChatModel([answer("ok")]),
        ),
        replace=True,
    )
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model="m", provider="test-fixed-window"
        ),
        workspace=Workspace.at(tmp_path),
        auto_classify=False,
    )
    with pytest.raises(ConfigurationError):
        session.set_context_window(65536)


def test_ollama_s_tool_call_parse_error_is_its_own_error(monkeypatch):
    """DR-039: told apart from a broken connection so the round can be retried."""
    from logic.errors import MalformedToolCallError

    sent: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _capturing(
            [{"error": "XML syntax error on line 3: element <function> closed by </parameter>"}],
            sent,
        ),
    )
    with pytest.raises(MalformedToolCallError):
        list(OllamaChatModel(_settings()).stream([{"role": "user", "content": "x"}]))
