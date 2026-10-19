"""Ollama provider: OpenAI-compatible inference, native enumeration (DR-015).

Ollama serves an OpenAI-compatible endpoint at ``/v1``, so inference needs no
special handling at all — this provider reuses the standard chat model
unchanged, exactly as DR-006 requires.

What it adds is the half the OpenAI-compatible surface cannot express. Ollama's
native ``/api/tags`` reports, per model, the capability list — including
whether the model can tool-call — and the model's architectural context
maximum. ``/api/ps`` reports something different and more important: the window
a *currently loaded* model is actually being served in.

Those are not the same number, and DR-022 exists because conflating them
breaks the product silently. ``/api/tags`` will happily report 262144 for a
model Ollama is serving in a 4096-token window, because Ollama sizes the window
from ``OLLAMA_CONTEXT_LENGTH`` (default 4096), not from the model. Nothing in
the OpenAI-compatible request can raise it — ``num_ctx`` is ignored on ``/v1``
— and an over-long request is not rejected but silently truncated from the
front, evicting the system prompt. So this catalog reports the *served* window
as the budget and keeps the maximum only as a diagnostic.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from .catalog import ModelInfo, fetch_json
from .settings import InferenceSettings

__all__ = ["OllamaCatalog", "OLLAMA_DEFAULT_BASE_URL", "native_root"]

#: Ollama's conventional address, OpenAI-compatible path.
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"

#: Capability string Ollama reports for models that can call tools.
_TOOLS_CAPABILITY = "tools"


class _Loaded(NamedTuple):
    """One entry of ``/api/ps``: the served window and how much is on the GPU."""

    context_length: int
    gpu_fraction: float | None

#: The window Ollama serves when ``OLLAMA_CONTEXT_LENGTH`` is not set. Used
#: only when no model is loaded, so ``/api/ps`` cannot be asked what the real
#: window is (DR-022). Assuming Ollama's own documented default is conservative
#: and self-correcting: once a model loads, the measured value replaces it.
OLLAMA_DEFAULT_SERVED_CONTEXT = 4096

#: How to raise it, quoted verbatim in the warning a capped window produces.
OLLAMA_CONTEXT_ENV = "OLLAMA_CONTEXT_LENGTH"


def native_root(base_url: str) -> str:
    """Map an OpenAI-compatible base URL to Ollama's native API root.

    ``http://host:11434/v1`` -> ``http://host:11434``. Anything that does not
    end in ``/v1`` is returned unchanged, so a reverse-proxied deployment is
    not silently rewritten into a URL that does not exist.
    """
    trimmed = base_url.rstrip("/")
    return trimmed[: -len("/v1")] if trimmed.endswith("/v1") else trimmed


class OllamaCatalog:
    """Enumerates models via Ollama's native ``/api/tags``."""

    def __init__(self, settings: InferenceSettings) -> None:
        self._settings = settings

    @property
    def root(self) -> str:
        return native_root(self._settings.base_url)

    def version(self) -> str:
        """Return the running Ollama version, for diagnostics."""
        payload = fetch_json(
            f"{self.root}/api/version", timeout=self._settings.discovery_timeout
        )
        return str(payload.get("version") or "")

    def served_context_lengths(self) -> dict[str, int]:
        """Map loaded model name -> the window it is actually being served in.

        ``/api/ps`` is the only place Ollama exposes this. A model that is not
        loaded is simply absent, which is why the caller needs a fallback
        rather than treating an empty result as "no cap" (DR-022).
        """
        return {name: loaded.context_length for name, loaded in self._loaded().items()}

    def _loaded(self) -> dict[str, _Loaded]:
        """What ``/api/ps`` says about each loaded model (DR-022, DR-034)."""
        try:
            payload = fetch_json(
                f"{self.root}/api/ps", timeout=self._settings.discovery_timeout
            )
        except Exception:  # noqa: BLE001 - a missing /api/ps must not break discovery
            return {}
        entries = payload.get("models")
        if not isinstance(entries, list):
            return {}
        loaded: dict[str, _Loaded] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")
            length = _positive_int(entry.get("context_length"))
            if name and length:
                size = _positive_int(entry.get("size"))
                in_vram = entry.get("size_vram")
                fraction = (
                    min(1.0, max(0.0, float(in_vram) / size))
                    if size and isinstance(in_vram, (int, float))
                    else None
                )
                loaded[str(name)] = _Loaded(length, fraction)
        return loaded

    def list_models(self) -> tuple[ModelInfo, ...]:
        payload = fetch_json(
            f"{self.root}/api/tags", timeout=self._settings.discovery_timeout
        )
        entries = payload.get("models")
        if not isinstance(entries, list):
            return ()
        loaded = self._loaded()
        models = [
            self._to_model_info(entry, loaded)
            for entry in entries
            if isinstance(entry, dict)
        ]
        return tuple(
            sorted(
                (model for model in models if model is not None),
                key=lambda model: model.name,
            )
        )

    @staticmethod
    def _to_model_info(
        entry: dict, loaded: dict[str, _Loaded] | None = None
    ) -> ModelInfo | None:
        name = entry.get("name") or entry.get("model")
        if not name:
            return None
        raw_details = entry.get("details")
        details: dict = raw_details if isinstance(raw_details, dict) else {}
        capabilities = entry.get("capabilities")
        # DR-022: `/api/tags` publishes the model's architectural maximum. The
        # budget must use what is actually served — measured from `/api/ps`
        # when the model is loaded, and otherwise Ollama's own default, capped
        # by the maximum so a genuinely small model is not overstated.
        maximum = _positive_int(details.get("context_length"))
        observed = (loaded or {}).get(str(name))
        measured = observed.context_length if observed else None
        assumed = (
            min(OLLAMA_DEFAULT_SERVED_CONTEXT, maximum)
            if maximum
            else OLLAMA_DEFAULT_SERVED_CONTEXT
        )
        return ModelInfo(
            name=str(name),
            context_length=measured or assumed,
            # `/api/ps` answered for this model, so the window is observed
            # rather than inferred from Ollama's default — which is what makes
            # it safe to refuse work over (DR-022).
            context_length_measured=measured is not None,
            max_context_length=maximum,
            # Absent capabilities means the runtime did not say, which is not
            # the same as saying no — older Ollama builds omit the field.
            supports_tools=(
                _TOOLS_CAPABILITY in capabilities
                if isinstance(capabilities, list)
                else None
            ),
            parameter_size=str(details.get("parameter_size") or ""),
            quantization=str(details.get("quantization_level") or ""),
            size_bytes=_positive_int(entry.get("size")),
            family=str(details.get("family") or ""),
            gpu_fraction=observed.gpu_fraction if observed else None,
        )


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None

