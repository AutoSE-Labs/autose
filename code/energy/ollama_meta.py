"""Fetch model metadata from Ollama for energy approximation."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse


def ollama_native_root(openai_base_url: str) -> str | None:
    """Map OpenAI-compat base URL to Ollama native root, if it looks like Ollama."""
    value = (openai_base_url or "").strip().rstrip("/")
    if not value:
        return None
    if value.endswith("/v1"):
        value = value[:-3]
    parsed = urlparse(value if "://" in value else f"http://{value}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    # Only probe native /api/show for typical local Ollama ports/hosts.
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port == 11434 or host in {"127.0.0.1", "localhost"}:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def fetch_model_meta(base_url: str, model: str, *, timeout: float = 2.0) -> dict[str, Any]:
    """
    Return model details for approximation.

    Prefers Ollama ``/api/show``. On failure returns whatever can be inferred from
    the model name alone (callers still parse signals).
    """
    meta: dict[str, Any] = {
        "parameter_size": None,
        "quantization_level": None,
        "family": None,
        "parameter_count": None,
        "size_bytes": None,
    }
    root = ollama_native_root(base_url)
    if root is None or not model:
        return meta
    payload = json.dumps({"model": model}).encode("utf-8")
    request = urllib.request.Request(
        f"{root}/api/show",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return meta

    details = body.get("details") if isinstance(body, dict) else None
    if isinstance(details, dict):
        meta["parameter_size"] = _as_str(details.get("parameter_size"))
        meta["quantization_level"] = _as_str(details.get("quantization_level"))
        meta["family"] = _as_str(details.get("family"))

    info = body.get("model_info") if isinstance(body, dict) else None
    if isinstance(info, dict):
        count = info.get("general.parameter_count")
        if isinstance(count, int) and count > 0:
            meta["parameter_count"] = count
            if not meta["parameter_size"]:
                billions = count / 1_000_000_000
                meta["parameter_size"] = f"{billions:.1f}B"

    # Optional: model blob size from tags list is not in show; leave size_bytes unset.
    return meta


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
