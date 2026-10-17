"""Provider registry — the selection half of the inference Strategy.

A *provider* bundles the two things a backend supplies: how to run a model
(``ChatModel``, DR-006) and how to enumerate what it serves (``ModelCatalog``,
DR-015). Registering both together is what lets a caller ask "what can I run
here?" without knowing which backend it is talking to.

Adding a backend is one module plus one ``register_provider`` call, with no
edit to any code that selects or uses one — DR-002's test for a real Strategy
seam.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..errors import ConfigurationError
from .base import ChatModel
from .catalog import ModelCatalog, OpenAICompatibleCatalog
from .ollama import OLLAMA_DEFAULT_BASE_URL, OllamaCatalog
from .openai_compatible import OpenAICompatibleChatModel
from .settings import DEFAULT_PROVIDER, InferenceSettings

__all__ = [
    "Provider",
    "register_provider",
    "get_provider",
    "available_providers",
    "local_providers",
    "create_chat_model",
    "create_model_catalog",
]

ChatModelFactory = Callable[[InferenceSettings], ChatModel]
CatalogFactory = Callable[[InferenceSettings], ModelCatalog]


@dataclass(frozen=True)
class Provider:
    """One registered inference backend."""

    name: str
    chat_model: ChatModelFactory
    catalog: CatalogFactory = OpenAICompatibleCatalog
    #: Whether this backend runs on the user's own machine. DR-015 makes local
    #: providers the ones surfaced first.
    is_local: bool = False
    #: Conventional address, used as the default and when probing (DR-015).
    default_base_url: str = ""
    description: str = ""


_PROVIDERS: dict[str, Provider] = {}


def register_provider(provider: Provider, *, replace: bool = False) -> None:
    """Register *provider* under its name.

    Re-registering a name requires ``replace=True``, so two backends claiming
    the same name is an error rather than a silent last-one-wins.
    """
    key = provider.name.strip().lower()
    if not key:
        raise ValueError("provider name must not be empty")
    if key in _PROVIDERS and not replace:
        raise ConfigurationError(f"inference provider '{key}' is already registered")
    _PROVIDERS[key] = provider


def get_provider(name: str) -> Provider:
    """Look up a registered provider, or explain what is available."""
    key = (name or DEFAULT_PROVIDER).strip().lower()
    provider = _PROVIDERS.get(key)
    if provider is None:
        known = ", ".join(sorted(_PROVIDERS)) or "none"
        raise ConfigurationError(
            f"unknown inference provider '{name}'. Registered: {known}."
        )
    return provider


def available_providers() -> Mapping[str, Provider]:
    """Every registered provider, keyed by name."""
    return dict(_PROVIDERS)


def local_providers() -> tuple[Provider, ...]:
    """Providers that run on the user's own machine, in name order (DR-015)."""
    return tuple(
        sorted(
            (provider for provider in _PROVIDERS.values() if provider.is_local),
            key=lambda provider: provider.name,
        )
    )


def create_chat_model(settings: InferenceSettings) -> ChatModel:
    """Build the ``ChatModel`` for ``settings.provider``."""
    return get_provider(settings.provider).chat_model(settings)


def create_model_catalog(settings: InferenceSettings) -> ModelCatalog:
    """Build the ``ModelCatalog`` for ``settings.provider``."""
    return get_provider(settings.provider).catalog(settings)


register_provider(
    Provider(
        name="ollama",
        chat_model=OpenAICompatibleChatModel,
        catalog=OllamaCatalog,
        is_local=True,
        default_base_url=OLLAMA_DEFAULT_BASE_URL,
        description="Ollama — local models, native catalog with context length and capabilities",
    )
)
register_provider(
    Provider(
        name="lm-studio",
        chat_model=OpenAICompatibleChatModel,
        is_local=True,
        default_base_url="http://localhost:1234/v1",
        description="LM Studio — local models via its OpenAI-compatible server",
    )
)
register_provider(
    Provider(
        name="vllm",
        chat_model=OpenAICompatibleChatModel,
        is_local=True,
        default_base_url="http://localhost:8000/v1",
        description="vLLM — self-hosted OpenAI-compatible server",
    )
)
register_provider(
    Provider(
        name="llama-cpp",
        chat_model=OpenAICompatibleChatModel,
        is_local=True,
        default_base_url="http://localhost:8080/v1",
        description="llama.cpp server — OpenAI-compatible endpoint",
    )
)
register_provider(
    Provider(
        name="openai-compatible",
        chat_model=OpenAICompatibleChatModel,
        is_local=False,
        description=(
            "Any OpenAI-compatible endpoint — set base_url, and api_key if the "
            "endpoint requires one"
        ),
    )
)
