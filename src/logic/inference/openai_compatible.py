"""The OpenAI-compatible chat-completions implementation of ``ChatModel``.

Deliberately built on ``urllib`` from the standard library rather than a
vendor SDK: DR-006 makes the wire shape the integration surface, and a
hand-rolled client keeps AutoSE installable with no provider package and no
opinion about which vendor's client library is present.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from ..errors import InferenceError, InferenceTimeoutError, ProviderContextLengthError
from .settings import InferenceSettings
from .types import CompletionResult, StreamChunk, ToolCall, Usage

__all__ = ["OpenAICompatibleChatModel"]

#: Substrings in a provider's error body that mean "your request was too long".
#: Matching on text is unavoidable — the OpenAI-compatible shape has no
#: standard machine-readable code for it — so keep this at the boundary, where
#: DR-006 says provider quirks belong.
_CONTEXT_ERROR_HINTS = ("context", "token", "length", "exceed", "maximum")

#: Field names under which runtimes return a reasoning model's chain of
#: thought. None of these is in the OpenAI chat-completions spec, which is
#: precisely why reading them belongs here at the boundary (DR-006, DR-016).
_REASONING_FIELDS = ("reasoning", "reasoning_content", "thinking")


class _ControlTokenFilter:
    """Strips chat-template control tokens from a streamed text channel.

    Some models leak raw template markers (``<|channel>``, ``<|end|>``) into
    their reasoning output. They arrive split across deltas, so a per-delta
    regex cannot catch them: text is held back from the moment an opening
    ``<|`` is seen until its closing ``>`` arrives.
    """

    #: Longest held-back fragment before we conclude the ``<|`` was literal
    #: text rather than an unterminated marker, and release it.
    _MAX_HELD = 64

    def __init__(self) -> None:
        self._held = ""

    def feed(self, text: str) -> str:
        buffer = self._held + text
        self._held = ""
        emitted: list[str] = []
        while buffer:
            start = buffer.find("<|")
            if start < 0:
                # A trailing lone "<" could begin a marker in the next delta.
                if buffer.endswith("<"):
                    self._held = "<"
                    buffer = buffer[:-1]
                emitted.append(buffer)
                break
            emitted.append(buffer[:start])
            remainder = buffer[start:]
            end = remainder.find(">")
            if end < 0:
                if len(remainder) > self._MAX_HELD:
                    emitted.append(remainder)
                else:
                    self._held = remainder
                break
            buffer = remainder[end + 1 :]
        return "".join(emitted)

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held


def _reasoning_of(payload: dict) -> str:
    for field in _REASONING_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


#: Gateway statuses a proxy in front of the runtime returns while its upstream
#: is briefly unreachable, plus 429: a rate limit (a hosted free tier's, say)
#: clears by itself, and giving up on it would end a long run on a blip.
_TRANSIENT_HTTP_CODES = frozenset({429, 502, 503, 504})
#: Seconds to wait before each retry of a transient failure (about 15 minutes
#: in all), after which the last error is raised as before.
_TRANSIENT_RETRY_DELAYS = (5, 10, 20, 40, 60, 60, 60, 60, 120, 120, 120, 120, 120)


class _TransientInferenceError(Exception):
    """A failure worth retrying; carries the error to raise if retries run out."""

    def __init__(self, final: Exception) -> None:
        super().__init__(str(final))
        self.final = final


def _is_connection_failure(exc: BaseException) -> bool:
    """A refused, reset or unreachable connection, not a slow response.

    A read timeout is not retried: the request already waited the full
    configured timeout, and repeating it would double a long stall.
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return True
    text = str(reason).lower()
    return any(
        needle in text
        for needle in ("connection refused", "connection reset", "network is unreachable",
                       "remote end closed", "no route to host")
    )


class OpenAICompatibleChatModel:
    """Speaks ``POST {base_url}/chat/completions`` and nothing else."""

    def __init__(self, settings: InferenceSettings) -> None:
        self._settings = settings

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def settings(self) -> InferenceSettings:
        return self._settings

    # -- public API ----------------------------------------------------

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> CompletionResult:
        payload = self._request_body(
            messages,
            tools,
            max_output_tokens,
            stream=False,
            reasoning_effort=reasoning_effort,
        )
        response = self._post(payload)
        return self._normalise(response)

    def stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[StreamChunk]:
        if not self._settings.stream:
            # Same chunks a stream would end with, from one ordinary request.
            result = self.complete(
                messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
            if result.reasoning:
                yield StreamChunk(reasoning=result.reasoning)
            if result.content:
                yield StreamChunk(content=result.content)
            yield StreamChunk(result=result)
            return
        payload = self._request_body(
            messages,
            tools,
            max_output_tokens,
            stream=True,
            reasoning_effort=reasoning_effort,
        )
        request = self._build_request(payload)
        accumulator = _StreamAccumulator()
        streamed = False
        try:
            # S310: the URL is the operator's own configured endpoint, not
            # anything a model supplied.
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self._settings.timeout
            ) as response:
                for raw_line in response:
                    chunk = self._parse_sse_line(raw_line)
                    if chunk is None:
                        continue
                    content, reasoning = accumulator.absorb(chunk)
                    if content or reasoning:
                        streamed = True
                        yield StreamChunk(content=content, reasoning=reasoning)
        except urllib.error.HTTPError as exc:
            # Not every OpenAI-compatible server implements SSE. Falling back
            # to one non-streaming call keeps those usable.
            self._raise_for_http_error(exc)
            result = self.complete(
                messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
            if result.content:
                yield StreamChunk(content=result.content)
            yield StreamChunk(result=result)
            return
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ) as exc:
            if not _is_connection_failure(exc) and not isinstance(
                exc, http.client.HTTPException
            ):
                raise InferenceTimeoutError(self._unreachable_message(exc)) from exc
            # A dropped connection, at the start or part-way through. Streaming
            # has no retry of its own, so repeat the request the way a
            # non-streaming one is repeated (with backoff). Anything already
            # shown stays shown; the final result below is authoritative.
            result = self.complete(
                messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
            if result.content and not streamed:
                yield StreamChunk(content=result.content)
            yield StreamChunk(result=result)
            return
        yield StreamChunk(result=accumulator.finish())

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
        body: dict[str, Any] = {
            "temperature": self._settings.temperature,
            **{
                key: value
                for key, value in self._settings.request_extras.items()
                if key not in ("model", "messages", "tools", "stream", "stream_options")
            },
            "model": self._settings.model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if max_output_tokens and max_output_tokens > 0:
            body["max_tokens"] = max_output_tokens
        if reasoning_effort:
            # Standard OpenAI field (DR-020); a runtime that ignores it is
            # unaffected, one that honours it skips reasoning altogether.
            body["reasoning_effort"] = reasoning_effort
        if stream:
            body["stream"] = True
            # Standard OpenAI option; runtimes that ignore unknown fields are
            # unaffected, and one that rejects it falls back to the
            # non-streaming path below, which reports usage anyway.
            body["stream_options"] = {"include_usage": True}
        return body

    def _build_request(self, body: dict) -> urllib.request.Request:
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        # S310: the base URL is the operator's own configured endpoint.
        return urllib.request.Request(  # noqa: S310
            f"{self._settings.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _post(self, body: dict) -> dict:
        """POST once, retrying transient gateway and connection failures.

        A proxy or tunnel between AutoSE and the runtime can drop for a few
        seconds; treating its 502 as fatal ended hour-long runs on a blip.
        """
        for delay in (*_TRANSIENT_RETRY_DELAYS, None):
            try:
                return self._post_once(body)
            except _TransientInferenceError as exc:
                if delay is None:
                    raise exc.final from exc.__cause__
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _post_once(self, body: dict) -> dict:
        request = self._build_request(body)
        try:
            with urllib.request.urlopen(  # noqa: S310 - configured endpoint
                request, timeout=self._settings.timeout
            ) as response:
                # `json.loads` is typed `Any`; a provider that answers with a
                # list or a bare string is a protocol violation, and saying so
                # here beats an AttributeError three frames later.
                decoded = json.loads(response.read().decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise InferenceError(
                        "inference backend returned a "
                        f"{type(decoded).__name__}, not a JSON object"
                    )
                return decoded
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_HTTP_CODES:
                final = InferenceError(f"inference backend returned HTTP {exc.code}")
                raise _TransientInferenceError(final) from exc
            self._raise_for_http_error(exc)
            raise InferenceError(f"inference backend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            final = InferenceTimeoutError(self._unreachable_message(exc))
            if _is_connection_failure(exc):
                raise _TransientInferenceError(final) from exc
            raise final from exc
        except json.JSONDecodeError as exc:
            raise InferenceError(
                f"inference backend returned a non-JSON response: {exc}"
            ) from exc

    def _unreachable_message(self, exc: Exception) -> str:
        return (
            f"could not reach the inference backend at {self._settings.base_url}: {exc}"
        )

    @staticmethod
    def _raise_for_http_error(exc: urllib.error.HTTPError) -> None:
        """Translate a context-window rejection into its own typed error."""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - body is best-effort diagnostics
            body = ""
        lowered = body.lower()
        if any(hint in lowered for hint in _CONTEXT_ERROR_HINTS):
            raise ProviderContextLengthError(body or f"HTTP {exc.code}") from exc

    # -- response normalisation ----------------------------------------

    @staticmethod
    def _normalise(response: dict) -> CompletionResult:
        choices = response.get("choices") or []
        if not choices:
            raise InferenceError("inference backend returned no choices")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        raw_calls = message.get("tool_calls") or []
        calls = tuple(
            ToolCall.from_payload(call, fallback_index=index)
            for index, call in enumerate(raw_calls)
            if isinstance(call, dict)
        )
        return CompletionResult(
            content=str(message.get("content") or ""),
            tool_calls=calls,
            usage=Usage.from_payload(response.get("usage")),
            finish_reason=str(choice.get("finish_reason") or ""),
            reasoning=_reasoning_of(message),
        )

    @staticmethod
    def _parse_sse_line(raw_line: bytes) -> dict | None:
        """Return the decoded payload of one SSE line, or ``None`` to skip it."""
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            return None
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


class _StreamAccumulator:
    """Reassembles a streamed turn — content and tool calls — from SSE deltas.

    Tool-call arguments arrive split across arbitrarily many chunks and are
    addressed by ``index`` rather than by ``id``, so they are stitched back
    together per index here. Doing this at the provider boundary is what lets
    the agent loop treat a streamed round exactly like a non-streamed one.
    """

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._reasoning_filter = _ControlTokenFilter()
        self._calls: dict[int, dict[str, str]] = {}
        self._finish_reason = ""
        self._usage = Usage()

    def absorb(self, payload: dict) -> tuple[str, str]:
        """Fold one chunk in, returning its ``(content, reasoning)`` deltas."""
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._usage = Usage.from_payload(usage)

        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return "", ""
        choice = choices[0]
        if choice.get("finish_reason"):
            self._finish_reason = str(choice["finish_reason"])

        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            return "", ""

        for entry in delta.get("tool_calls") or []:
            if isinstance(entry, dict):
                self._absorb_tool_call(entry)

        content = delta.get("content")
        content = content if isinstance(content, str) else ""
        if content:
            self._content.append(content)

        reasoning = self._reasoning_filter.feed(_reasoning_of(delta))
        if reasoning:
            self._reasoning.append(reasoning)

        return content, reasoning

    def _absorb_tool_call(self, entry: dict) -> None:
        index = entry.get("index")
        slot = self._calls.setdefault(
            int(index) if isinstance(index, int) else len(self._calls),
            {"id": "", "name": "", "arguments": ""},
        )
        if entry.get("id"):
            slot["id"] = str(entry["id"])
        function = entry.get("function") or {}
        if not isinstance(function, dict):
            return
        if function.get("name"):
            slot["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments

    def finish(self) -> CompletionResult:
        trailing = self._reasoning_filter.flush()
        if trailing:
            self._reasoning.append(trailing)
        calls = tuple(
            ToolCall.from_payload(
                {
                    "id": slot["id"],
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"],
                    },
                },
                fallback_index=index,
            )
            for index, slot in sorted(self._calls.items())
            if slot["name"]
        )
        return CompletionResult(
            content="".join(self._content),
            tool_calls=calls,
            usage=self._usage,
            finish_reason=self._finish_reason,
            reasoning="".join(self._reasoning),
        )
