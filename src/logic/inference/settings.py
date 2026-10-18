"""Connection settings for an OpenAI-compatible endpoint (DR-006).

Three fields carry the whole bring-your-own-LLM contract: where the endpoint
is, how to authenticate (optionally — a local runtime usually needs no key),
and which model to ask for. Everything else is tuning.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..errors import ConfigurationError

__all__ = [
    "InferenceSettings",
    "DEFAULT_PROVIDER",
    "DEFAULT_BASE_URL",
    "DEFAULT_REQUEST_TIMEOUT",
    "DEFAULT_DISCOVERY_TIMEOUT",
]

#: The provider used when configuration names none. DR-015 makes this the
#: local runtime, not a hosted API.
DEFAULT_PROVIDER = "ollama"

#: Where AutoSE looks with no configuration at all: Ollama's conventional
#: port, OpenAI-compatible path (DR-015).
DEFAULT_BASE_URL = "http://localhost:11434/v1"

#: Generous by hosted-API standards, because a local model on weak hardware
#: legitimately takes minutes for a long generation (DR-015). A stuck request
#: still eventually raises rather than hanging the process forever.
DEFAULT_REQUEST_TIMEOUT = 900.0

#: Probing and enumeration must stay snappy — these run before the user has
#: asked for anything, so a dead port must not stall startup.
DEFAULT_DISCOVERY_TIMEOUT = 2.0


@dataclass(frozen=True)
class InferenceSettings:
    """Everything needed to reach one chat-completions endpoint."""

    base_url: str = DEFAULT_BASE_URL
    #: May be empty until a model is chosen — discovery can fill it in, so an
    #: unset model is a normal pre-selection state rather than an error (DR-015).
    model: str = ""
    #: Empty is the normal case for a local runtime; no key is requested or
    #: required unless the endpoint is a hosted one (DR-015).
    api_key: str = ""
    #: Registered provider implementation name (see :mod:`logic.inference.registry`).
    provider: str = DEFAULT_PROVIDER
    #: Total context window in tokens, if known. ``None`` disables token-based
    #: budgeting and falls back to a fixed history cap (DR-010).
    context_limit: int | None = None
    temperature: float = 0.2
    #: Per-request socket timeout for inference. ``None`` waits indefinitely.
    timeout: float | None = DEFAULT_REQUEST_TIMEOUT
    #: Timeout for enumeration and probing, which must never stall startup.
    discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT
    #: ``False`` sends each round as one non-streaming request. Usage is then
    #: always reported, and a proxy that buffers or times out long SSE
    #: responses is not in the way. Rounds produce the same result either way.
    stream: bool = True
    #: Extra fields merged into every chat-completions request body, for
    #: provider options AutoSE has no setting for — sampling (``top_p``,
    #: ``top_k``) or ``chat_template_kwargs`` such as Qwen's
    #: ``enable_thinking``. They never override the messages, model, tools or
    #: stream fields.
    request_extras: dict[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if not self.base_url or not self.base_url.strip():
            raise ConfigurationError("inference.base_url is required")
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))
        object.__setattr__(self, "model", (self.model or "").strip())
        object.__setattr__(self, "provider", (self.provider or DEFAULT_PROVIDER).strip())

    @property
    def is_ready(self) -> bool:
        """Whether a model has been chosen, so inference can actually run."""
        return bool(self.model)

    def require_model(self) -> InferenceSettings:
        """Return self, or explain that a model still has to be selected."""
        if not self.model:
            raise ConfigurationError(
                f"no model selected for {self.base_url}. Choose one from the "
                "runtime's available models, or set inference.model."
            )
        return self

    def with_model(self, model: str) -> InferenceSettings:
        return replace(self, model=model)

    def with_context_limit(self, context_limit: int | None) -> InferenceSettings:
        return replace(self, context_limit=context_limit)
