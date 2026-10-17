"""Model enumeration — the discovery half of a provider (DR-015).

Runtimes differ sharply in what they will tell you about a model. The
OpenAI-compatible ``/v1/models`` endpoint returns bare identifiers and nothing
else; a native endpoint may publish the real context length, the parameter
count, the quantisation, and whether the model can tool-call at all.

That difference is exactly why enumeration is its own Strategy seam rather
than a method bolted onto ``ChatModel``: a provider supplies whichever catalog
it can honestly back, and callers degrade gracefully when a field is unknown
instead of inventing one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..errors import InferenceTimeoutError
from .settings import InferenceSettings

__all__ = ["ModelInfo", "ModelCatalog", "OpenAICompatibleCatalog", "fetch_json"]


@dataclass(frozen=True)
class ModelInfo:
    """What a runtime is willing to tell us about one model it serves.

    Every field beyond :attr:`name` is optional, because the OpenAI-compatible
    endpoint supplies none of them. ``None`` means *unknown*, never *absent* —
    a caller must not read "does not support tools" out of a missing
    capability list.
    """

    name: str
    #: The window the runtime will **actually serve** for this model, in
    #: tokens, when that is knowable. DR-010's budget uses this in preference
    #: to any configured or assumed value.
    #:
    #: DR-022: this is deliberately *not* the model's architectural maximum.
    #: A runtime routinely serves far less than the model could support —
    #: Ollama defaults to 4096 regardless of a 256k-capable model — and
    #: budgeting against the maximum silently overflows every request.
    context_length: int | None = None
    #: Whether :attr:`context_length` was *observed* or merely *assumed*.
    #:
    #: Ollama only publishes the window it is serving for a model that is
    #: currently loaded (`/api/ps`); for a cold one, all AutoSE can do is
    #: assume its 4096 default. The two must not be treated alike. Refusing
    #: to run on an observation is honest; refusing on a guess would lock a
    #: user out of a server that was correctly configured all along and had
    #: simply not been asked for anything yet.
    context_length_measured: bool = False
    #: The largest window the model itself supports, when published. Display
    #: and diagnostics only: it is an upper bound on what a runtime *could* be
    #: configured to serve, never a budget (DR-022).
    max_context_length: int | None = None
    #: Whether the model can call tools. ``None`` when the runtime does not say.
    supports_tools: bool | None = None
    #: Human-facing size, e.g. "11.9B". Display only.
    parameter_size: str = ""
    #: Human-facing quantisation, e.g. "Q4_K_M". Display only.
    quantization: str = ""
    #: On-disk size in bytes, when known.
    size_bytes: int | None = None
    family: str = ""

    @property
    def is_tool_capable(self) -> bool:
        """True only when the runtime affirmatively says tools are supported.

        Unknown counts as not-confirmed, so a pre-flight check warns rather
        than promising a capability nobody vouched for.
        """
        return self.supports_tools is True

    @property
    def is_context_capped(self) -> bool:
        """True when the runtime serves less than the model could support.

        The signal behind DR-022's startup warning: the user has a 256k-capable
        model being served in a 4k window, and nothing but this comparison
        would tell them.
        """
        return (
            self.context_length is not None
            and self.max_context_length is not None
            and self.max_context_length > self.context_length
        )

    def describe(self) -> str:
        """A one-line summary for a picker or a status line."""
        parts = [self.name]
        detail = [
            value
            for value in (
                self.parameter_size,
                self.quantization,
                f"{self.context_length // 1024}k ctx" if self.context_length else "",
            )
            if value
        ]
        if detail:
            parts.append(f"({', '.join(detail)})")
        if self.supports_tools is False:
            parts.append("[no tool calling]")
        return " ".join(parts)


@runtime_checkable
class ModelCatalog(Protocol):
    """Strategy seam: how a provider enumerates the models it serves."""

    def list_models(self) -> tuple[ModelInfo, ...]:
        """Return the available models. Raises on an unreachable endpoint."""


class OpenAICompatibleCatalog:
    """Enumerates via ``GET {base_url}/models`` — names only.

    The floor every OpenAI-compatible provider can meet. A runtime that
    publishes more registers a richer catalog instead.
    """

    def __init__(self, settings: InferenceSettings) -> None:
        self._settings = settings

    def list_models(self) -> tuple[ModelInfo, ...]:
        headers = {"Accept": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        payload = fetch_json(
            f"{self._settings.base_url}/models",
            headers=headers,
            timeout=self._settings.discovery_timeout,
        )
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return ()
        models = [
            ModelInfo(name=str(entry["id"]))
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        ]
        return tuple(sorted(models, key=lambda model: model.name))


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float | None = 5.0,
) -> dict:
    """GET (or POST, with *data*) a JSON document, raising a typed error.

    Shared by every catalog and by runtime probing, so "the endpoint is not
    there" surfaces the same way regardless of which one asked.
    """
    # S310: the URL is the operator's own configured endpoint, never
    # anything a model supplied.
    request = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers=headers or {"Accept": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        # S310: the URL is the operator's own configured endpoint, not
        # anything a model supplied.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InferenceTimeoutError(f"could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InferenceTimeoutError(f"{url} returned a non-JSON response: {exc}") from exc
    return payload if isinstance(payload, dict) else {}
