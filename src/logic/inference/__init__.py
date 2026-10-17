"""Provider-agnostic inference, local-first by default (DR-006, DR-015).

AutoSE never bundles or proxies a model. It targets any OpenAI-compatible
chat-completions endpoint, and defaults to finding one already running on the
user's own machine rather than asking them to describe one.
"""

from __future__ import annotations

from .base import ChatModel
from .catalog import ModelCatalog, ModelInfo, OpenAICompatibleCatalog
from .discovery import DetectedRuntime, discover_local_runtimes, probe_runtime
from .ollama import OllamaCatalog
from .openai_compatible import OpenAICompatibleChatModel
from .registry import (
    Provider,
    available_providers,
    create_chat_model,
    create_model_catalog,
    get_provider,
    local_providers,
    register_provider,
)
from .settings import (
    DEFAULT_BASE_URL,
    DEFAULT_DISCOVERY_TIMEOUT,
    DEFAULT_PROVIDER,
    DEFAULT_REQUEST_TIMEOUT,
    InferenceSettings,
)
from .types import CompletionResult, StreamChunk, ToolCall, Usage

__all__ = [
    "ChatModel",
    "CompletionResult",
    "DEFAULT_BASE_URL",
    "DEFAULT_DISCOVERY_TIMEOUT",
    "DEFAULT_PROVIDER",
    "DEFAULT_REQUEST_TIMEOUT",
    "DetectedRuntime",
    "InferenceSettings",
    "ModelCatalog",
    "ModelInfo",
    "OllamaCatalog",
    "OpenAICompatibleCatalog",
    "OpenAICompatibleChatModel",
    "Provider",
    "StreamChunk",
    "ToolCall",
    "Usage",
    "available_providers",
    "create_chat_model",
    "create_model_catalog",
    "discover_local_runtimes",
    "get_provider",
    "local_providers",
    "probe_runtime",
    "register_provider",
]
