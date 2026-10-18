"""DR-036 over HTTP: the settings page's providers, keys and default.

A key goes browser -> server in a POST body and never comes back: every answer,
and every state snapshot, carries only its masked form.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

pytest.importorskip("gui", reason="the GUI adapter is a Pro-edition build")

from gui.bridge import BridgeError, SessionBridge
from gui.server import create_dashboard
from logic.config import AutoSEConfig
from logic.credentials import CredentialStore
from logic.errors import InferenceTimeoutError
from logic.inference import ollama_cloud
from logic.inference.catalog import ModelInfo
from logic.inference.registry import Provider, get_provider, register_provider
from logic.inference.settings import InferenceSettings
from tests.support.fakes import ScriptedChatModel, answer

_KEY = "sk-secret-cloud-key-5678wxyz"
_TOKEN = "test-token-1234"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A local test runtime, a fake ollama.com, and every other port closed."""
    monkeypatch.setattr(ollama_cloud, "_shown", {})
    cloud_requests: list[dict] = []

    def fake_fetch(url, *, headers=None, data=None, timeout=5.0):
        if url.startswith("https://ollama.com/"):
            cloud_requests.append(dict(headers or {}))
        if url == "https://ollama.com/api/tags":
            return {"models": [{"name": "gpt-oss:120b"}]}
        if url == "https://ollama.com/api/show":
            return {
                "model_info": {"gptoss.context_length": 131072},
                "capabilities": ["tools"],
            }
        raise InferenceTimeoutError(f"could not reach {url}: refused")

    for module in ("catalog", "ollama", "ollama_cloud"):
        monkeypatch.setattr(f"logic.inference.{module}.fetch_json", fake_fetch)

    built: list[InferenceSettings] = []
    local = ModelInfo(name="local-model", context_length=32768, supports_tools=True)

    class _Catalog:
        def __init__(self, settings):
            pass

        def list_models(self):
            return (local,)

    def chat_model(settings):
        built.append(settings)
        return ScriptedChatModel([answer(f"from {settings.provider}")])

    register_provider(
        Provider(
            name="test-settings",
            chat_model=chat_model,
            catalog=_Catalog,
            is_local=True,
        ),
        replace=True,
    )
    # The cloud's chat model is scripted too: nothing here reaches ollama.com.

    real = get_provider("ollama-cloud")
    register_provider(
        Provider(
            name="ollama-cloud",
            chat_model=chat_model,
            catalog=real.catalog,
            default_base_url=real.default_base_url,
            description=real.description,
            requires_api_key=True,
            api_key_env=real.api_key_env,
        ),
        replace=True,
    )
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="http://test/v1", provider="test-settings", model="local-model"
        ),
        workspace_root=tmp_path,
    )
    bridge = SessionBridge(config, default_tier="spark")
    return {"bridge": bridge, "built": built, "cloud": cloud_requests}


@pytest.fixture
def served(world):
    dashboard = create_dashboard(world["bridge"], host="127.0.0.1", port=0, token=_TOKEN)
    thread = threading.Thread(target=dashboard.serve_forever, daemon=True)
    thread.start()
    try:
        yield dashboard
    finally:
        dashboard.stop()
        dashboard.server_close()


def _request(dashboard, path, *, body=None):
    port = dashboard.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"X-AutoSE-Token": _TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw), raw.decode()
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw), raw.decode()


def _row(payload, name):
    return next(row for row in payload["providers"] if row["provider"] == name)


def test_the_providers_listing_shows_every_provider_and_no_key(served):
    status, payload, _ = _request(served, "/api/providers")
    assert status == 200
    cloud = _row(payload, "ollama-cloud")
    assert cloud["requires_api_key"] is True
    assert cloud["has_key"] is False
    assert "API key" in cloud["error"]
    local = _row(payload, "test-settings")
    assert local["reachable"] and local["models"][0]["name"] == "local-model"
    assert payload["default"]["provider"] == "test-settings"


def test_a_saved_key_comes_back_masked_and_nowhere_in_full(served, world):
    status, payload, raw = _request(
        served,
        "/api/providers",
        body={"action": "save_key", "provider": "ollama-cloud", "api_key": _KEY},
    )
    assert status == 200
    cloud = _row(payload, "ollama-cloud")
    assert cloud["has_key"] and cloud["masked_key"] == "••••wxyz"
    assert cloud["key_source"] == "stored"
    assert cloud["models"][0]["name"] == "gpt-oss:120b"
    assert _KEY not in raw
    # The catalog was the one place the key went.
    assert all(headers["Authorization"] == f"Bearer {_KEY}" for headers in world["cloud"])

    _, _, listing = _request(served, "/api/providers")
    _, _, state = _request(served, "/api/state")
    assert _KEY not in listing
    assert _KEY not in state


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_key_saved_from_the_dashboard_is_owner_only(served):
    _request(
        served,
        "/api/providers",
        body={"action": "save_key", "provider": "ollama-cloud", "api_key": _KEY},
    )
    path = CredentialStore().path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_deleting_a_key_is_reflected_and_a_missing_one_is_404(served):
    _request(
        served,
        "/api/providers",
        body={"action": "save_key", "provider": "ollama-cloud", "api_key": _KEY},
    )
    status, payload, _ = _request(
        served, "/api/providers", body={"action": "delete_key", "provider": "ollama-cloud"}
    )
    assert status == 200
    assert _row(payload, "ollama-cloud")["has_key"] is False
    status, _, _ = _request(
        served, "/api/providers", body={"action": "delete_key", "provider": "ollama-cloud"}
    )
    assert status == 404


@pytest.mark.parametrize(
    "body",
    [
        {"action": "explode", "provider": "ollama-cloud"},
        {"action": "save_key", "provider": "ollama-cloud"},
        {"action": "save_key", "provider": "ollama-cloud", "api_key": 12345},
        {"action": "save_key", "provider": "ollama-cloud", "api_key": "a b\nc"},
        {"action": "save_key", "provider": "nonesuch", "api_key": _KEY},
        {"action": "save_key", "api_key": _KEY},
        {"action": "set_default", "provider": "ollama-cloud"},
        {"action": "set_default", "provider": "test-settings", "model": "m", "base_url": "file:///etc"},
    ],
    ids=[
        "unknown-action",
        "no-key",
        "key-not-a-string",
        "key-with-whitespace",
        "unknown-provider",
        "no-provider",
        "default-without-model",
        "default-with-non-http-url",
    ],
)
def test_bad_provider_requests_are_refused_with_400(served, body):
    status, payload, raw = _request(served, "/api/providers", body=body)
    assert status == 400
    assert payload["error"]
    assert _KEY not in raw


def test_the_default_is_saved_to_the_user_config_without_the_key(served, world):
    _request(
        served,
        "/api/providers",
        body={"action": "save_key", "provider": "ollama-cloud", "api_key": _KEY},
    )
    status, payload, _ = _request(
        served,
        "/api/providers",
        body={"action": "set_default", "provider": "ollama-cloud", "model": "gpt-oss:120b"},
    )
    assert status == 200
    assert payload["default"] == {
        "provider": "ollama-cloud",
        "base_url": "https://ollama.com",
        "model": "gpt-oss:120b",
    }
    saved = Path(payload["saved_to"]).read_text(encoding="utf-8")
    assert _KEY not in saved
    assert yaml.safe_load(saved)["inference"]["provider"] == "ollama-cloud"

    # Open sessions keep their provider; a new one starts on the default.
    _, state, _ = _request(served, "/api/state")
    assert state["provider"] == "test-settings"
    _, state, _ = _request(served, "/api/session", body={"action": "new"})
    assert state["provider"] == "ollama-cloud"
    assert world["built"][-1].api_key == _KEY


def test_a_hosted_default_without_a_key_is_refused(served):
    status, payload, _ = _request(
        served,
        "/api/providers",
        body={"action": "set_default", "provider": "ollama-cloud", "model": "gpt-oss:120b"},
    )
    assert status == 400
    assert "API key" in payload["error"]


def test_one_session_switches_provider_and_the_others_do_not(served, world):
    _request(
        served,
        "/api/providers",
        body={"action": "save_key", "provider": "ollama-cloud", "api_key": _KEY},
    )
    _, first, _ = _request(served, "/api/state")
    _request(served, "/api/session", body={"action": "new"})
    status, _, raw = _request(
        served,
        "/api/model",
        body={
            "model": "gpt-oss:120b",
            "provider": "ollama-cloud",
            "session": first["session_id"],
        },
    )
    assert status == 200
    assert _KEY not in raw
    _, focused, _ = _request(served, "/api/state")
    assert focused["session_id"] != first["session_id"]
    # The focused (second) session is untouched.
    assert focused["provider"] == "test-settings"
    bridge = world["bridge"]
    switched = bridge._open[first["session_id"]].session
    assert switched.inference.provider == "ollama-cloud"
    assert switched.inference.api_key == _KEY
    assert switched.context_limit() == 131072
    _, focus_state, _ = _request(
        served, "/api/session", body={"action": "focus", "id": first["session_id"]}
    )
    assert focus_state["provider"] == "ollama-cloud"
    assert [m["name"] for m in focus_state["models"]] == ["gpt-oss:120b"]


def test_switching_to_a_provider_without_a_key_is_a_400(served):
    status, payload, _ = _request(
        served, "/api/model", body={"model": "gpt-oss:120b", "provider": "ollama-cloud"}
    )
    assert status == 400
    assert "API key" in payload["error"]


def test_a_running_session_cannot_switch_provider(world):
    bridge: SessionBridge = world["bridge"]
    CredentialStore().save("ollama-cloud", _KEY)
    live = next(iter(bridge._open.values()))
    live.running = True
    with pytest.raises(BridgeError) as refused:
        bridge.select_model("gpt-oss:120b", provider="ollama-cloud")
    assert refused.value.status == 409
    assert live.session.inference.provider == "test-settings"


def test_a_dashboard_whose_default_needs_a_key_opens_once_one_is_saved(
    tmp_path, world
):
    config = AutoSEConfig(
        inference=InferenceSettings(
            base_url="https://ollama.com", provider="ollama-cloud", model="gpt-oss:120b"
        ),
        workspace_root=tmp_path,
    )
    bridge = SessionBridge(config, default_tier="spark")
    state = bridge.snapshot()
    assert state["ready"] is False
    assert "API key" in state["problem"]
    bridge.save_key("ollama-cloud", _KEY)
    assert bridge.snapshot()["ready"] is True


def test_switching_back_to_the_default_provider_restores_its_address(served, world):
    """The default may live somewhere other than its provider's usual port (an
    SSH tunnel on :11435, say). Leaving it for a hosted provider and coming
    back must land on that address, not on the provider's conventional one."""
    _request(
        served,
        "/api/providers",
        body={"action": "save_key", "provider": "ollama-cloud", "api_key": _KEY},
    )
    _request(served, "/api/model", body={"model": "gpt-oss:120b", "provider": "ollama-cloud"})
    status, state, _ = _request(
        served, "/api/model", body={"model": "local-model", "provider": "test-settings"}
    )
    assert status == 200
    assert state["provider"] == "test-settings"
    assert state["base_url"] == "http://test/v1"
