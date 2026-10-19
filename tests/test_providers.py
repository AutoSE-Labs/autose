"""DR-036: keys in an owner-only file, Ollama Cloud, and switching per session."""

from __future__ import annotations

import io
import json
import os
import stat

import pytest
import yaml

from logic import config as config_module
from logic.common.events import RecordingEventSink
from logic.common.workspace import Workspace
from logic.config import AutoSEConfig, load_config
from logic.connections import list_connections
from logic.credentials import CredentialStore, mask_key
from logic.errors import ConfigurationError, InferenceTimeoutError
from logic.inference import ollama_cloud
from logic.inference.catalog import ModelInfo
from logic.inference.ollama_chat import OllamaChatModel
from logic.inference.registry import (
    Provider,
    available_providers,
    create_model_catalog,
    get_provider,
    register_provider,
)
from logic.inference.settings import InferenceSettings
from logic.session import Session, setup_for_provider, setup_from_config
from logic.user_config import save_default_inference, user_config_dir
from tests.support.fakes import ScriptedChatModel, answer

_KEY = "sk-ollama-0123456789abcdef"

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


# -- the credentials file ---------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> CredentialStore:
    return CredentialStore(tmp_path / "autose" / "credentials.yaml")


def test_the_credentials_file_lives_beside_the_user_config(isolated_user_config):
    assert CredentialStore().path == isolated_user_config / "autose" / "credentials.yaml"
    assert user_config_dir() == isolated_user_config / "autose"


@posix_only
def test_a_saved_key_is_readable_by_its_owner_only(store):
    store.save("ollama-cloud", _KEY)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


@posix_only
def test_rewriting_the_file_keeps_it_owner_only(store):
    store.save("ollama-cloud", _KEY)
    store.path.chmod(0o644)
    store.save("openai-compatible", "sk-other-0000000000")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_previous_file_and_no_temporary(store, monkeypatch):
    store.save("ollama-cloud", _KEY)
    before = store.path.read_text()

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        store.save("ollama-cloud", "sk-new-key-9999999999")
    assert store.path.read_text() == before
    assert [p.name for p in store.path.parent.iterdir()] == ["credentials.yaml"]


def test_a_key_is_shown_only_masked(store):
    store.save("ollama-cloud", _KEY)
    view = store.view("ollama-cloud")
    assert view.has_key and view.source == "stored"
    assert view.masked == "••••cdef"
    assert _KEY not in repr(view)


def test_a_short_key_shows_no_characters_at_all():
    assert mask_key("abcd1234") == "••••"


def test_deleting_a_key_forgets_it(store):
    store.save("ollama-cloud", _KEY)
    assert store.delete("ollama-cloud") is True
    assert store.stored("ollama-cloud") == ""
    assert store.view("ollama-cloud").has_key is False
    assert store.delete("ollama-cloud") is False


def test_the_conventional_environment_variable_is_a_fallback(store, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-from-env-11112222")
    assert store.resolve("ollama-cloud") == "sk-from-env-11112222"
    assert store.view("ollama-cloud").source == "environment"
    store.save("ollama-cloud", _KEY)
    assert store.resolve("ollama-cloud") == _KEY


def test_a_key_that_cannot_be_a_header_is_refused(store):
    with pytest.raises(ConfigurationError):
        store.save("ollama-cloud", "sk-abc\r\nX-Evil: 1")
    with pytest.raises(ConfigurationError):
        store.save("ollama-cloud", "   ")
    assert not store.path.exists()


# -- configuration picks the key up -----------------------------------------------


def _write_config(path, inference: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"inference": inference}))


def test_the_configured_provider_gets_its_stored_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSE_API_KEY", raising=False)
    CredentialStore().save("ollama-cloud", _KEY)
    path = tmp_path / "c.yaml"
    _write_config(path, {"provider": "ollama-cloud", "model": "gpt-oss:120b"})
    config = load_config(path, workspace_root=tmp_path)
    assert config.inference.api_key == _KEY
    # Naming the provider is enough to reach it (DR-036).
    assert config.inference.base_url == "https://ollama.com"


def test_autose_api_key_still_wins_for_the_configured_provider(tmp_path, monkeypatch):
    CredentialStore().save("ollama-cloud", _KEY)
    monkeypatch.setenv("AUTOSE_API_KEY", "sk-explicit-override")
    path = tmp_path / "c.yaml"
    _write_config(path, {"provider": "ollama-cloud", "model": "m"})
    assert load_config(path, workspace_root=tmp_path).inference.api_key == (
        "sk-explicit-override"
    )


def test_a_hosted_default_without_a_key_is_not_ready_and_says_why(tmp_path):
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="https://ollama.com", provider="ollama-cloud", model="m"
        ),
        workspace_root=tmp_path,
    )
    setup = setup_from_config(config)
    assert not setup.is_ready
    assert "API key" in setup.warnings[0]


# -- the default for new sessions -------------------------------------------------


def test_the_default_is_saved_without_the_key_and_other_settings_survive(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "inference": {"provider": "ollama", "temperature": 0.5},
                "tuning": {"context": {"safety_factor": 1.2}},
            }
        )
    )
    save_default_inference(
        "ollama-cloud", "gpt-oss:120b", "https://ollama.com", path=path
    )
    document = yaml.safe_load(path.read_text())
    assert document["inference"] == {
        "provider": "ollama-cloud",
        "temperature": 0.5,
        "base_url": "https://ollama.com",
        "model": "gpt-oss:120b",
    }
    assert document["tuning"] == {"context": {"safety_factor": 1.2}}
    assert "api_key" not in path.read_text()


def test_the_default_goes_to_the_user_config_file(isolated_user_config):
    target = save_default_inference("ollama", "qwen3:8b", "http://localhost:11434/v1")
    assert target == isolated_user_config / "autose" / "config.yaml"
    assert config_module.find_config_file() == target


def test_a_default_with_no_model_is_refused(tmp_path):
    with pytest.raises(ConfigurationError):
        save_default_inference("ollama", "", "http://x", path=tmp_path / "c.yaml")


# -- Ollama Cloud -----------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def cloud(monkeypatch):
    """A fake ollama.com: `/api/tags`, `/api/show`, `/api/chat`; records requests."""
    monkeypatch.setattr(ollama_cloud, "_shown", {})
    sent: list = []

    def fake_urlopen(request, timeout=None):
        sent.append(request)
        url = request.full_url
        if url == "https://ollama.com/api/tags":
            body = {
                "models": [
                    {"name": "gpt-oss:120b", "details": {"family": "gptoss"}},
                    {"name": "kimi-k2:1t", "details": {}},
                ]
            }
        elif url == "https://ollama.com/api/show":
            name = json.loads(request.data)["model"]
            body = (
                {
                    "model_info": {"gptoss.context_length": 131072},
                    "capabilities": ["completion", "tools"],
                }
                if name == "gpt-oss:120b"
                else {"capabilities": ["completion"]}
            )
        elif url == "https://ollama.com/api/chat":
            body = {"message": {"content": "hi from the cloud"}, "done": True}
        else:
            raise AssertionError(f"unexpected request to {url}")
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return sent


def _cloud(**overrides) -> InferenceSettings:
    values = {
        "base_url": "https://ollama.com",
        "provider": "ollama-cloud",
        "api_key": _KEY,
        "model": "gpt-oss:120b",
    }
    values.update(overrides)
    return InferenceSettings(**values)


def test_ollama_cloud_is_hosted_and_needs_a_key():
    provider = get_provider("ollama-cloud")
    assert provider.requires_api_key and not provider.is_local
    assert provider.default_base_url == "https://ollama.com"
    assert provider.api_key_env == "OLLAMA_API_KEY"
    # The window is not observable in the cloud, so it is not claimed (DR-035).
    assert provider.serves_requested_window is False


def test_the_cloud_catalog_authenticates_and_lists_models(cloud):
    models = create_model_catalog(_cloud()).list_models()
    assert [model.name for model in models] == ["gpt-oss:120b", "kimi-k2:1t"]
    assert all(r.get_header("Authorization") == f"Bearer {_KEY}" for r in cloud)
    # There is no `/api/ps` to ask in the cloud.
    assert not any(r.full_url.endswith("/api/ps") for r in cloud)


def test_a_cloud_model_is_budgeted_at_its_published_window(cloud):
    gpt, kimi = create_model_catalog(_cloud()).list_models()
    assert gpt.context_length == gpt.max_context_length == 131072
    assert gpt.supports_tools is True
    assert gpt.gpu_fraction is None
    # Nothing published: a conservative assumption, marked as one.
    assert kimi.context_length == ollama_cloud.OLLAMA_CLOUD_ASSUMED_CONTEXT
    assert kimi.context_length_measured is False


def test_a_v1_base_url_reaches_the_same_native_api(cloud):
    create_model_catalog(_cloud(base_url="https://ollama.com/v1")).list_models()
    assert cloud[0].full_url == "https://ollama.com/api/tags"


def test_a_cloud_chat_sends_the_key_to_the_native_endpoint(cloud):
    result = OllamaChatModel(_cloud()).complete([{"role": "user", "content": "hi"}])
    (request,) = cloud
    assert request.full_url == "https://ollama.com/api/chat"
    assert request.get_header("Authorization") == f"Bearer {_KEY}"
    assert result.content == "hi from the cloud"


# -- the connections listing ------------------------------------------------------


@pytest.fixture
def offline(monkeypatch):
    """Every local runtime is down; only the fake cloud answers."""
    monkeypatch.setattr(ollama_cloud, "_shown", {})
    asked: list[tuple[str, dict]] = []

    def fake_fetch(url, *, headers=None, data=None, timeout=5.0):
        asked.append((url, dict(headers or {})))
        if url == "https://ollama.com/api/tags":
            return {"models": [{"name": "gpt-oss:120b"}]}
        if url == "https://ollama.com/api/show":
            return {"capabilities": ["tools"]}
        raise InferenceTimeoutError(f"could not reach {url}: refused")

    for module in ("catalog", "ollama", "ollama_cloud"):
        monkeypatch.setattr(f"logic.inference.{module}.fetch_json", fake_fetch)
    return asked


def _row(connections, name):
    return next(row for row in connections if row.provider == name)


def test_every_registered_provider_is_listed_local_first(offline, store):
    connections = list_connections(credentials=store)
    assert {row.provider for row in connections} == set(available_providers())
    locals_first = [row.is_local for row in connections]
    assert locals_first == sorted(locals_first, reverse=True)


def test_a_hosted_provider_without_a_key_is_not_contacted(offline, store):
    row = _row(list_connections(credentials=store), "ollama-cloud")
    assert not row.reachable and "API key" in row.error
    assert not any("ollama.com" in url for url, _ in offline)


def test_with_a_key_the_cloud_lists_its_models(offline, store):
    store.save("ollama-cloud", _KEY)
    row = _row(list_connections(credentials=store), "ollama-cloud")
    assert row.reachable and [m.name for m in row.models] == ["gpt-oss:120b"]
    assert row.key.masked == "••••cdef"
    assert _KEY not in repr(row)


def test_one_broken_provider_does_not_hide_the_others(offline, store):
    class _Exploding:
        def __init__(self, settings):
            pass

        def list_models(self):
            raise RuntimeError("boom")

    register_provider(
        Provider(
            name="test-exploding",
            chat_model=lambda s: ScriptedChatModel([]),
            catalog=_Exploding,
            default_base_url="http://explode/v1",
        )
    )
    connections = list_connections(credentials=store)
    assert not _row(connections, "test-exploding").reachable
    assert "boom" in _row(connections, "test-exploding").error
    assert len(connections) == len(available_providers())


# -- switching provider within one session ------------------------------------------


@pytest.fixture
def two_providers():
    """A keyless local runtime and a hosted one that needs a key."""
    built: dict[str, list] = {"settings": []}
    local = ModelInfo(name="local-model", context_length=32768, supports_tools=True)
    hosted = ModelInfo(
        name="hosted-model",
        context_length=131072,
        context_length_measured=True,
        max_context_length=131072,
        supports_tools=True,
    )

    def catalog_of(*models):
        class _Catalog:
            def __init__(self, settings):
                pass

            def list_models(self):
                return models

        return _Catalog

    def chat_model(settings):
        built["settings"].append(settings)
        return ScriptedChatModel([answer(f"from {settings.provider}")])

    register_provider(
        Provider(
            name="test-local",
            chat_model=chat_model,
            catalog=catalog_of(local),
            is_local=True,
            default_base_url="http://localhost:9/v1",
        )
    )
    register_provider(
        Provider(
            name="test-hosted",
            chat_model=chat_model,
            catalog=catalog_of(hosted),
            default_base_url="https://hosted.example",
            requires_api_key=True,
        )
    )
    return built


def _local_session(tmp_path, store, events=None) -> Session:
    return Session(
        inference=InferenceSettings(
            base_url="http://localhost:9/v1", provider="test-local", model="local-model"
        ),
        workspace=Workspace.at(tmp_path),
        events=events,
        auto_classify=False,
        credentials=store,
    )


def test_a_session_switches_to_another_provider_with_its_stored_key(
    two_providers, tmp_path, store
):
    store.save("test-hosted", _KEY)
    events = RecordingEventSink()
    session = _local_session(tmp_path, store, events)
    session.select_model("hosted-model", provider="test-hosted")

    inference = session.inference
    assert (inference.provider, inference.base_url, inference.model) == (
        "test-hosted",
        "https://hosted.example",
        "hosted-model",
    )
    assert inference.api_key == _KEY
    # The window is decided afresh for the new model (DR-035).
    assert session.context_limit() == 131072
    (selected,) = events.of_type("model_selected")
    assert selected.data["provider"] == "test-hosted"
    assert selected.data["previous_provider"] == "test-local"
    assert _KEY not in json.dumps(selected.data)
    # The next turn really goes to the new provider.
    assert session.run("hello").answer == "from test-hosted"


def test_a_switch_to_a_provider_with_no_key_is_refused_and_changes_nothing(
    two_providers, tmp_path, store
):
    session = _local_session(tmp_path, store)
    before = session.inference
    with pytest.raises(ConfigurationError, match="API key"):
        session.use_provider("test-hosted", "hosted-model")
    assert session.inference == before


def test_switching_back_needs_no_key_for_a_local_runtime(
    two_providers, tmp_path, store
):
    store.save("test-hosted", _KEY)
    session = _local_session(tmp_path, store)
    session.use_provider("test-hosted", "hosted-model")
    session.use_provider("test-local", "local-model")
    assert session.inference.provider == "test-local"
    assert session.inference.base_url == "http://localhost:9/v1"
    assert session.inference.api_key == ""


def test_an_unknown_provider_is_refused(two_providers, tmp_path, store):
    session = _local_session(tmp_path, store)
    with pytest.raises(ConfigurationError, match="unknown inference provider"):
        session.use_provider("nonesuch", "m")


def test_a_default_on_another_provider_resolves_like_a_configured_one(
    two_providers, tmp_path, store
):
    store.save("test-hosted", _KEY)
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="http://localhost:9/v1",
            provider="test-local",
            model="local-model",
            context_limit=16384,
        ),
        workspace_root=tmp_path,
    )
    setup = setup_for_provider(config, "test-hosted", "hosted-model", credentials=store)
    assert setup.is_ready
    assert setup.inference is not None
    assert setup.inference.api_key == _KEY
    # The configured window belonged to the other provider.
    assert setup.inference.context_limit == 131072
