"""Ollama's native chat endpoint as a ``ChatModel`` (DR-035).

Ollama's OpenAI-compatible ``/v1/chat/completions`` ignores ``num_ctx``: the
window a model is served in can only be set when the server starts, through
``OLLAMA_CONTEXT_LENGTH``, and nothing a client sends changes it. The native
``/api/chat`` takes the window per request (``options.num_ctx``). Speaking it is
what lets AutoSE choose the window itself — from configuration or from the
dashboard — on a laptop or on a remote server it cannot restart.

The rest of AutoSE still sees OpenAI-shaped messages and normalised results
(DR-006): the translation both ways happens here, at the boundary.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from ..errors import (
    InferenceError,
    InferenceTimeoutError,
    MalformedToolCallError,
    ProviderContextLengthError,
)
from .ollama import native_root
from .openai_compatible import OpenAICompatibleChatModel
from .types import CompletionResult, StreamChunk, ToolCall, Usage

__all__ = ["OllamaChatModel", "to_native_messages"]

_CONTEXT_ERROR_HINTS = ("context", "num_ctx", "too long", "exceed")

#: Ollama's own parser rejecting the tool call a model wrote — seen in practice
#: as "XML syntax error on line 3: element <function> closed by </parameter>"
#: from a Qwen model (DR-039).
_TOOL_CALL_ERROR_HINTS = ("xml syntax error", "tool call", "parsing tool", "parse tool")


class OllamaChatModel(OpenAICompatibleChatModel):
    """``ChatModel`` over ``/api/chat``, requesting the window it budgets for.

    Inherits authentication and HTTP error translation from the OpenAI client;
    replaces the URL, the request body, response parsing and the stream format
    (newline-delimited JSON rather than SSE).
    """

    # -- request construction ------------------------------------------

    def _request_body(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        max_output_tokens: int | None,
        *,
        stream: bool,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": self._settings.temperature}
        if self._settings.context_limit and self._settings.context_limit > 0:
            # The point of this class: the window AutoSE budgets against is the
            # window the runtime is asked to serve (DR-035).
            options["num_ctx"] = self._settings.context_limit
        if max_output_tokens and max_output_tokens > 0:
            options["num_predict"] = max_output_tokens
        body: dict[str, Any] = {
            "model": self._settings.model,
            "messages": to_native_messages(messages),
            "stream": stream,
            "options": options,
        }
        if tools:
            body["tools"] = tools
        if reasoning_effort == "none":
            # DR-020's "no reasoning" request, in Ollama's own vocabulary. Any
            # other effort leaves the model's default thinking behaviour alone.
            body["think"] = False
        return body

    def _build_request(self, body: dict) -> urllib.request.Request:
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        # S310: the base URL is the operator's own configured endpoint.
        return urllib.request.Request(  # noqa: S310
            f"{native_root(self._settings.base_url)}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    # -- calls -----------------------------------------------------------

    def stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[StreamChunk]:
        payload = self._request_body(
            messages,
            tools,
            max_output_tokens,
            stream=True,
            reasoning_effort=reasoning_effort,
        )
        request = self._build_request(payload)
        accumulator = _NativeAccumulator()
        try:
            with urllib.request.urlopen(  # noqa: S310 - configured endpoint
                request, timeout=self._settings.timeout
            ) as response:
                for raw_line in response:
                    chunk = _parse_ndjson_line(raw_line)
                    if chunk is None:
                        continue
                    content, reasoning = accumulator.absorb(chunk)
                    if content or reasoning:
                        yield StreamChunk(content=content, reasoning=reasoning)
        except urllib.error.HTTPError as exc:
            self._raise_for_http_error(exc)
            raise InferenceError(f"inference backend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InferenceTimeoutError(self._unreachable_message(exc)) from exc
        yield StreamChunk(result=accumulator.finish())

    @staticmethod
    def _raise_for_http_error(exc: urllib.error.HTTPError) -> None:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body is best-effort diagnostics
            body = ""
        _raise_for_error_text(body)

    @staticmethod
    def _normalise(response: dict) -> CompletionResult:
        accumulator = _NativeAccumulator()
        accumulator.absorb({**response, "done": True})
        return accumulator.finish()


def to_native_messages(messages: list[dict]) -> list[dict]:
    """Translate OpenAI-shaped history into ``/api/chat`` messages.

    Two differences matter: native tool-call arguments are an object, not a
    JSON string, and a tool result names its tool (``tool_name``) instead of
    pointing at a call id — so each result is matched to the call it answers.
    """
    names_by_id: dict[str, str] = {}
    native: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = _text_of(message.get("content"))
        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": content}
            calls = []
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                if call.get("id"):
                    names_by_id[str(call["id"])] = name
                calls.append(
                    {"function": {"name": name, "arguments": _arguments_of(function)}}
                )
            if calls:
                entry["tool_calls"] = calls
            native.append(entry)
        elif role == "tool":
            entry = {"role": "tool", "content": content}
            tool_name = names_by_id.get(str(message.get("tool_call_id") or ""))
            if tool_name:
                entry["tool_name"] = tool_name
            native.append(entry)
        else:
            native.append({"role": str(role or "user"), "content": content})
    return native


class _NativeAccumulator:
    """Reassembles one ``/api/chat`` turn from NDJSON chunks (or one response)."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: list[ToolCall] = []
        self._usage = Usage()
        self._done_reason = ""

    def absorb(self, payload: dict) -> tuple[str, str]:
        if payload.get("error"):
            _raise_for_error_text(str(payload["error"]))
            raise InferenceError(f"ollama: {payload['error']}")
        message = payload.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        reasoning = message.get("thinking")
        reasoning = reasoning if isinstance(reasoning, str) else ""
        if content:
            self._content.append(content)
        if reasoning:
            self._reasoning.append(reasoning)
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                # Native calls carry no id; a stable per-turn index pairs each
                # with its result exactly as the OpenAI shape does.
                self._calls.append(
                    ToolCall.from_payload(
                        call, fallback_index=len(self._calls)
                    )
                )
        if payload.get("done"):
            self._done_reason = str(payload.get("done_reason") or "")
            self._usage = Usage(
                prompt_tokens=int(payload.get("prompt_eval_count") or 0),
                completion_tokens=int(payload.get("eval_count") or 0),
            )
        return content, reasoning

    def finish(self) -> CompletionResult:
        return CompletionResult(
            content="".join(self._content),
            tool_calls=tuple(self._calls),
            usage=self._usage,
            finish_reason="tool_calls" if self._calls else self._done_reason,
            reasoning="".join(self._reasoning),
        )


def _parse_ndjson_line(raw_line: bytes) -> dict | None:
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _raise_for_error_text(text: str) -> None:
    lowered = text.lower()
    if any(hint in lowered for hint in _TOOL_CALL_ERROR_HINTS):
        raise MalformedToolCallError(text)
    if any(hint in lowered for hint in _CONTEXT_ERROR_HINTS):
        raise ProviderContextLengthError(text or "context window exceeded")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _arguments_of(function: dict) -> dict:
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
