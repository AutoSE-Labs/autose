"""Ollama Cloud: Ollama's own API, hosted, behind a key (DR-036).

``https://ollama.com/api`` speaks the same native protocol as a local server —
``GET /api/tags`` lists models, ``POST /api/chat`` runs one — so inference
reuses :class:`~logic.inference.ollama_chat.OllamaChatModel` unchanged; it
already sends ``Authorization: Bearer <key>`` when the settings carry a key.
No local Ollama has to be running.

What differs is what the service will tell us about the window:

* there is no ``/api/ps`` to ask. The window a cloud model is served in is not
  observable, and nothing about GPU residency applies;
* whether ``options.num_ctx`` is honoured is not documented. AutoSE sends it
  anyway — the native client always does — but cannot verify it, so the
  provider does **not** claim ``serves_requested_window`` (DR-035): the
  dashboard offers no window choices, and the budget is the model's published
  maximum, or :data:`OLLAMA_CLOUD_ASSUMED_CONTEXT` where none is published.

The maximum comes from ``/api/show``, asked once per model and remembered for
the life of the process, because it does not change and a listing of thirty
models should not cost thirty round trips every time the settings page opens.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .catalog import ModelInfo, fetch_json
from .context_advice import COMFORTABLE_CONTEXT_TOKENS
from .ollama import native_root
from .settings import InferenceSettings

__all__ = [
    "OLLAMA_CLOUD_BASE_URL",
    "OLLAMA_CLOUD_KEY_ENV",
    "OLLAMA_CLOUD_ASSUMED_CONTEXT",
    "OllamaCloudCatalog",
]

#: The service root. ``native_root`` leaves it as-is; ``…/v1`` would work too.
OLLAMA_CLOUD_BASE_URL = "https://ollama.com"

#: The variable Ollama's own tools read the key from.
OLLAMA_CLOUD_KEY_ENV = "OLLAMA_API_KEY"

#: The budget for a cloud model that publishes no maximum. Cloud models are
#: large and serve long windows; this is the floor DR-034 calls comfortable,
#: not a claim about any one model.
OLLAMA_CLOUD_ASSUMED_CONTEXT = COMFORTABLE_CONTEXT_TOKENS

_TOOLS_CAPABILITY = "tools"

#: `/api/show` lookups run side by side, a few at a time.
_SHOW_WORKERS = 8

_shown: dict[tuple[str, str], tuple[int | None, bool | None]] = {}
_shown_lock = threading.Lock()


class OllamaCloudCatalog:
    """Enumerates ``https://ollama.com/api/tags``, authenticated."""

    def __init__(self, settings: InferenceSettings) -> None:
        self._settings = settings

    @property
    def root(self) -> str:
        return native_root(self._settings.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    def list_models(self) -> tuple[ModelInfo, ...]:
        payload = fetch_json(
            f"{self.root}/api/tags",
            headers=self._headers(),
            timeout=self._settings.discovery_timeout,
        )
        entries = payload.get("models")
        if not isinstance(entries, list):
            return ()
        named = [
            entry
            for entry in entries
            if isinstance(entry, dict) and (entry.get("name") or entry.get("model"))
        ]
        names = [str(entry.get("name") or entry.get("model")) for entry in named]
        shown = self._show_all(names)
        models = [
            _to_model_info(entry, name, shown.get(name))
            for entry, name in zip(named, names, strict=True)
        ]
        return tuple(sorted(models, key=lambda model: model.name))

    # -- /api/show -----------------------------------------------------------

    def _show_all(self, names: list[str]) -> dict[str, tuple[int | None, bool | None]]:
        with _shown_lock:
            known = {name: _shown[(self.root, name)] for name in names if (self.root, name) in _shown}
        missing = [name for name in names if name not in known]
        if missing:
            with ThreadPoolExecutor(max_workers=min(_SHOW_WORKERS, len(missing))) as pool:
                for name, facts in zip(missing, pool.map(self._show, missing), strict=True):
                    if facts is not None:
                        known[name] = facts
        return known

    def _show(self, name: str) -> tuple[int | None, bool | None] | None:
        """One model's maximum window and tool support. ``None`` on any failure."""
        try:
            payload = fetch_json(
                f"{self.root}/api/show",
                headers={**self._headers(), "Content-Type": "application/json"},
                data=json.dumps({"model": name}).encode("utf-8"),
                timeout=self._settings.discovery_timeout,
            )
        except Exception:  # noqa: BLE001 - enrichment must never break a listing
            return None
        facts = (_published_context(payload), _tool_support(payload.get("capabilities")))
        with _shown_lock:
            _shown[(self.root, name)] = facts
        return facts


def _to_model_info(
    entry: dict, name: str, shown: tuple[int | None, bool | None] | None
) -> ModelInfo:
    raw_details = entry.get("details")
    details: dict = raw_details if isinstance(raw_details, dict) else {}
    maximum = (shown[0] if shown else None) or _positive_int(details.get("context_length"))
    supports_tools = shown[1] if shown else None
    if supports_tools is None:
        supports_tools = _tool_support(entry.get("capabilities"))
    return ModelInfo(
        name=name,
        # No `/api/ps` in the cloud: the window served is not observable, so the
        # budget is the published maximum, or a conservative assumption.
        context_length=maximum or OLLAMA_CLOUD_ASSUMED_CONTEXT,
        # Published by the service for this model, so not a guess; an assumed
        # window stays marked as one and is never refused over (DR-022).
        context_length_measured=maximum is not None,
        max_context_length=maximum,
        supports_tools=supports_tools,
        parameter_size=str(details.get("parameter_size") or ""),
        quantization=str(details.get("quantization_level") or ""),
        size_bytes=_positive_int(entry.get("size")),
        family=str(details.get("family") or ""),
        # Nothing about GPU residency is knowable for a hosted model.
        gpu_fraction=None,
    )


def _published_context(payload: dict) -> int | None:
    """``<architecture>.context_length`` from ``/api/show``'s ``model_info``."""
    info = payload.get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if str(key).endswith(".context_length"):
            number = _positive_int(value)
            if number:
                return number
    return None


def _tool_support(capabilities: Any) -> bool | None:
    if not isinstance(capabilities, list):
        return None
    return _TOOLS_CAPABILITY in capabilities


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
