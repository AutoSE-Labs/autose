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
    # Only probe native APIs for typical local Ollama ports/hosts.
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port == 11434 or host in {"127.0.0.1", "localhost"}:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def fetch_model_meta(base_url: str, model: str, *, timeout: float = 2.0) -> dict[str, Any]:
    """
    Return model details for approximation.

    Prefers Ollama ``/api/show`` for params/quant and ``/api/tags`` for blob size.
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

    _fill_from_show(root, model, meta, timeout=timeout)
    if meta["size_bytes"] is None:
        meta["size_bytes"] = _size_from_tags(root, model, timeout=timeout)
    return meta


def is_model_loaded(base_url: str, model: str, *, timeout: float = 1.0) -> bool | None:
    """
    Return whether ``model`` appears in Ollama ``/api/ps``.

    ``None`` means the probe failed (unknown); callers should not assume cold/warm.
    """
    root = ollama_native_root(base_url)
    if root is None or not model:
        return None
    body = _get_json(f"{root}/api/ps", timeout=timeout)
    if not isinstance(body, dict):
        return None
    models = body.get("models")
    if not isinstance(models, list):
        return None
    target = _normalize_model_name(model)
    for entry in models:
        if not isinstance(entry, dict):
            continue
        for key in ("model", "name"):
            value = entry.get(key)
            if isinstance(value, str) and _normalize_model_name(value) == target:
                return True
    return False


def extract_native_timings(payload: object) -> dict[str, int | None]:
    """Pull Ollama native timing fields if a response includes them."""
    if not isinstance(payload, dict):
        return {
            "load_duration_ns": None,
            "prompt_eval_duration_ns": None,
            "eval_duration_ns": None,
            "total_duration_ns": None,
        }
    # Native /api/chat fields, or rare OpenAI-compat extensions.
    source = payload
    for nest in ("timings", "ollama", "x_ollama"):
        nested = payload.get(nest)
        if isinstance(nested, dict):
            source = nested
            break
    return {
        "load_duration_ns": _as_nonneg_int(source.get("load_duration")),
        "prompt_eval_duration_ns": _as_nonneg_int(source.get("prompt_eval_duration")),
        "eval_duration_ns": _as_nonneg_int(source.get("eval_duration")),
        "total_duration_ns": _as_nonneg_int(source.get("total_duration")),
    }


def _fill_from_show(
    root: str, model: str, meta: dict[str, Any], *, timeout: float
) -> None:
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
        return

    if not isinstance(body, dict):
        return

    details = body.get("details")
    if isinstance(details, dict):
        meta["parameter_size"] = _as_str(details.get("parameter_size"))
        meta["quantization_level"] = _as_str(details.get("quantization_level"))
        meta["family"] = _as_str(details.get("family"))

    info = body.get("model_info")
    if isinstance(info, dict):
        count = info.get("general.parameter_count")
        if isinstance(count, int) and count > 0:
            meta["parameter_count"] = count
            if not meta["parameter_size"]:
                billions = count / 1_000_000_000
                meta["parameter_size"] = f"{billions:.1f}B"

    size = body.get("size")
    if isinstance(size, int) and size > 0:
        meta["size_bytes"] = size


def _size_from_tags(root: str, model: str, *, timeout: float) -> int | None:
    body = _get_json(f"{root}/api/tags", timeout=timeout)
    if not isinstance(body, dict):
        return None
    models = body.get("models")
    if not isinstance(models, list):
        return None
    target = _normalize_model_name(model)
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not isinstance(name, str):
            continue
        if _normalize_model_name(name) != target:
            continue
        size = entry.get("size")
        if isinstance(size, int) and size > 0:
            return size
    return None


def _get_json(url: str, *, timeout: float) -> Any | None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _normalize_model_name(name: str) -> str:
    value = name.strip().lower()
    if ":" not in value:
        value = f"{value}:latest"
    return value


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _as_nonneg_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    return None
