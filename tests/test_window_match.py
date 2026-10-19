"""DR-059: a window nobody chose follows the one the model is already loaded with."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.config import AutoSEConfig
from logic.inference.catalog import LoadedWindow, ModelInfo
from logic.inference.ollama import OllamaCatalog
from logic.inference.registry import Provider, register_provider
from logic.inference.settings import InferenceSettings
from logic.session import Session, SessionSetup
from tests.support.fakes import ScriptedChatModel, answer


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _fake_ollama(ps_models: list[dict] | None, asked: list[str]):
    """A fake Ollama that answers ``/api/ps``; ``None`` is a server that is down."""

    def fake_urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        asked.append(url)
        if ps_models is None:
            raise urllib.error.URLError("connection refused")
        if url.endswith("/api/ps"):
            return _Response(json.dumps({"models": ps_models}).encode("utf-8"))
        raise AssertionError(f"only /api/ps may be asked here, not {url}")

    return fake_urlopen


def _ps(name: str, window: int, *, vram_share: float = 1.0) -> dict:
    size = 24_000_000_000
    return {
        "name": name,
        "model": name,
        "context_length": window,
        "size": size,
        "size_vram": int(size * vram_share),
    }


_INFO = ModelInfo(
    name="qwen3.6:35b-a3b",
    context_length=32768,
    context_length_measured=True,
    max_context_length=262144,
)


def _ollama_session(events=None, **kwargs) -> Session:
    return Session(
        inference=InferenceSettings(
            base_url="http://localhost:11434/v1",
            model=_INFO.name,
            provider="ollama",
            context_limit=32768,
        ),
        workspace=Workspace.at(kwargs.pop("root")),
        model_info=_INFO,
        events=events,
        auto_classify=False,
        **kwargs,
    )


# -- reading the loaded window -----------------------------------------------------


def test_ollama_says_what_window_a_model_is_loaded_with(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_ollama([_ps("qwen3.6:35b-a3b", 65536), _ps("llama3:latest", 8192)], asked),
    )
    catalog = OllamaCatalog(InferenceSettings(base_url="http://h:11435/v1", provider="ollama"))
    assert catalog.loaded_window("qwen3.6:35b-a3b") == LoadedWindow(65536, 1.0)
    # A name without a tag is Ollama's `:latest`.
    assert catalog.loaded_window("llama3") == LoadedWindow(8192, 1.0)
    assert catalog.loaded_window("gemma4:12b") is None
    # Only the cheap listing is asked, never the model itself.
    assert set(asked) == {"http://h:11435/api/ps"}


def test_a_server_that_cannot_be_reached_reports_nothing_loaded(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _fake_ollama(None, []))
    catalog = OllamaCatalog(InferenceSettings(base_url="http://h/v1", provider="ollama"))
    assert catalog.loaded_window("qwen3.6:35b-a3b") is None


# -- matching it ---------------------------------------------------------------


def test_a_window_another_client_loaded_is_adopted_instead_of_reloading(
    monkeypatch, tmp_path
):
    """Without DR-059 the next request asks for 32k and Ollama reloads the model."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _fake_ollama([_ps("qwen3.6:35b-a3b", 65536, vram_share=0.9)], []),
    )
    events = RecordingEventSink()
    session = _ollama_session(events, root=tmp_path)
    assert session.match_loaded_window()
    assert session.context_limit() == 65536
    # The chat model now asks for 65536, which is what is loaded: no reload.
    assert session._model._settings.context_limit == 65536
    assert session.window_matched
    assert session.model_info.gpu_fraction == pytest.approx(0.9)
    (changed,) = events.of_type("context_window_changed")
    assert changed.data == {
        "previous": 32768,
        "tokens": 65536,
        "reason": "matched the loaded model",
    }


@pytest.mark.parametrize(
    ("loaded", "why"),
    [
        (None, "not loaded"),
        (32768, "already the same"),
        (4096, "below what AutoSE can work in"),
        (524288, "past the model's maximum"),
    ],
)
def test_a_window_that_is_not_worth_matching_is_left_alone(
    monkeypatch, tmp_path, loaded, why
):
    models = [_ps("qwen3.6:35b-a3b", loaded)] if loaded else [_ps("other:1b", 65536)]
    monkeypatch.setattr("urllib.request.urlopen", _fake_ollama(models, []))
    events = RecordingEventSink()
    session = _ollama_session(events, root=tmp_path)
    assert not session.match_loaded_window(), why
    assert session.context_limit() == 32768
    assert not events.of_type("context_window_changed")


def test_a_window_chosen_in_the_picker_always_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_ollama([_ps("qwen3.6:35b-a3b", 65536)], [])
    )
    events = RecordingEventSink()
    session = _ollama_session(events, root=tmp_path)
    # Choosing the window already in use reloads nothing and says nothing...
    session.set_context_window(32768)
    assert not events.of_type("context_window_changed")
    # ...but it is a choice now, so the loaded 64k does not move it.
    assert not session.match_loaded_window()
    assert session.context_limit() == 32768


def test_a_configured_window_always_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_ollama([_ps("qwen3.6:35b-a3b", 65536)], [])
    )
    session = _ollama_session(root=tmp_path, configured_window=32768)
    assert not session.match_loaded_window()
    assert session.context_limit() == 32768


def test_a_configured_window_reaches_the_session_from_the_setup(tmp_path):
    configured = InferenceSettings(
        base_url="http://localhost:11434/v1",
        model=_INFO.name,
        provider="ollama",
        context_limit=16384,
    )
    setup = SessionSetup(
        config=AutoSEConfig(inference=configured, workspace_root=tmp_path),
        inference=configured,
        model_info=_INFO,
    )
    assert Session.from_setup(setup)._configured_window == 16384
    # A default on another provider started from its own window.
    other = SessionSetup(
        config=AutoSEConfig(
            inference=InferenceSettings(provider="vllm", context_limit=16384),
            workspace_root=tmp_path,
        ),
        inference=configured,
        model_info=_INFO,
    )
    assert Session.from_setup(other)._configured_window is None


def test_a_runtime_that_serves_a_fixed_window_is_never_matched(monkeypatch, tmp_path):
    asked: list[str] = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_ollama([], asked))
    register_provider(
        Provider(
            name="test-fixed-match",
            chat_model=lambda settings: ScriptedChatModel([answer("ok")]),
        ),
        replace=True,
    )
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1", model="m", provider="test-fixed-match"
        ),
        workspace=Workspace.at(tmp_path),
        auto_classify=False,
    )
    assert not session.match_loaded_window()
    assert asked == []


# -- when it happens ---------------------------------------------------------------


@pytest.fixture
def loaded_runtime():
    """A provider that, like Ollama, serves what it is asked and says what is loaded."""
    holder: dict = {"built": [], "loaded": None}

    def factory(settings):
        holder["built"].append(settings)
        return ScriptedChatModel([answer("ok"), answer("ok")])

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return ()

        def loaded_window(self, name):
            return holder["loaded"]

    register_provider(
        Provider(
            name="test-loaded-window",
            chat_model=factory,
            catalog=_Catalog,
            serves_requested_window=True,
        ),
        replace=True,
    )
    return holder


def test_each_turn_starts_by_matching_the_loaded_window(loaded_runtime, tmp_path):
    events = RecordingEventSink()
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model="m",
            provider="test-loaded-window",
            context_limit=32768,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=ModelInfo(name="m", context_length=32768, max_context_length=131072),
        events=events,
        auto_classify=False,
    )
    loaded_runtime["loaded"] = LoadedWindow(65536, 1.0)
    session.run("hello")
    # The turn's requests went to a model built for the loaded window.
    assert loaded_runtime["built"][-1].context_limit == 65536
    kinds = [event.type for event in events.events]
    assert kinds.index("context_window_changed") < kinds.index("turn_started")

    # Unloaded since: the window stays where it is rather than guessing.
    loaded_runtime["loaded"] = None
    built = len(loaded_runtime["built"])
    session.run("again")
    assert session.context_limit() == 65536
    assert len(loaded_runtime["built"]) == built


def test_switching_models_keeps_a_configured_window(loaded_runtime, tmp_path):
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model="m",
            provider="test-loaded-window",
            context_limit=16384,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=ModelInfo(name="m", context_length=16384, max_context_length=131072),
        auto_classify=False,
        configured_window=16384,
    )
    session.select_model("n")
    assert session.context_limit() == 16384


# -- a configured window belongs to the configured provider -------------------------


@pytest.fixture
def fixed_runtime():
    """A provider that serves the window it was started with, 8k for every model."""

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (
                ModelInfo(name="a", context_length=8192),
                ModelInfo(name="b", context_length=8192),
            )

    register_provider(
        Provider(
            name="test-fixed-elsewhere",
            chat_model=lambda settings: ScriptedChatModel([answer("ok")]),
            catalog=_Catalog,
            default_base_url="http://fixed/v1",
        ),
        replace=True,
    )


def test_a_configured_window_is_not_applied_to_another_providers_model(
    loaded_runtime, fixed_runtime, tmp_path
):
    """Switching models on a provider that serves 8k must not claim 64k (DR-022)."""
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model="m",
            provider="test-loaded-window",
            context_limit=65536,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=ModelInfo(name="m", context_length=65536, max_context_length=131072),
        auto_classify=False,
        configured_window=65536,
    )
    session.select_model("a", provider="test-fixed-elsewhere")
    assert session.context_limit() == 8192
    session.select_model("b")
    assert session.context_limit() == 8192
    # Back on the provider the window was configured for, it applies again.
    session.select_model("m", provider="test-loaded-window", base_url="http://test/v1")
    assert session.context_limit() == 65536


def test_a_window_configured_for_another_provider_does_not_stop_matching(
    loaded_runtime, tmp_path
):
    """The configured window pins its own provider, not every one switched to."""

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (ModelInfo(name="m", context_length=32768, max_context_length=131072),)

        def loaded_window(self, name):
            return loaded_runtime["loaded"]

    register_provider(
        Provider(
            name="test-loaded-window-two",
            chat_model=lambda settings: ScriptedChatModel([answer("ok")]),
            catalog=_Catalog,
            default_base_url="http://two/v1",
            serves_requested_window=True,
        ),
        replace=True,
    )
    session = Session(
        inference=InferenceSettings(
            base_url="http://test/v1",
            model="m",
            provider="test-loaded-window",
            context_limit=16384,
        ),
        workspace=Workspace.at(tmp_path),
        model_info=ModelInfo(name="m", context_length=16384, max_context_length=131072),
        auto_classify=False,
        configured_window=16384,
    )
    session.select_model("m", provider="test-loaded-window-two")
    assert session.context_limit() == 32768
    loaded_runtime["loaded"] = LoadedWindow(65536, 1.0)
    assert session.match_loaded_window()
    assert session.context_limit() == 65536


# -- the workspace file is the workspace's own --------------------------------------


@pytest.mark.parametrize("linked", ["file", "directory"])
def test_a_workspace_file_that_links_outside_is_not_written(tmp_path, linked):
    """A repository could plant `.autose` as a link to a private file elsewhere.

    Saving through it would copy that file, secrets and all, into the
    workspace as a plain file the agent may read and a commit may carry.
    """
    from logic.config import save_workspace_context_limit
    from logic.errors import ConfigurationError

    outside = tmp_path / "home"
    outside.mkdir()
    private = outside / "config.yaml"
    private.write_text("token: s3cret\n")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    if linked == "file":
        (workspace / ".autose").mkdir()
        (workspace / ".autose" / "config.yaml").symlink_to(private)
    else:
        (workspace / ".autose").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigurationError):
        save_workspace_context_limit(workspace, 65536)
    assert private.read_text() == "token: s3cret\n"
    assert (workspace / ".autose" / "config.yaml").is_symlink() == (linked == "file")
    assert "s3cret" not in "".join(
        path.read_text() for path in workspace.rglob("*") if path.is_file()
        and not path.is_symlink() and outside not in path.resolve().parents
    )


def test_a_workspace_file_linked_inside_the_workspace_is_refused_too(tmp_path):
    """Replacing the link would quietly detach it from the file it shares."""
    from logic.config import save_workspace_context_limit
    from logic.errors import ConfigurationError

    (tmp_path / "shared.yaml").write_text("inference:\n  temperature: 0.2\n")
    (tmp_path / ".autose").mkdir()
    (tmp_path / ".autose" / "config.yaml").symlink_to(tmp_path / "shared.yaml")
    with pytest.raises(ConfigurationError):
        save_workspace_context_limit(tmp_path, 65536)
