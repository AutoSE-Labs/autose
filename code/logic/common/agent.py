import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path


class ContextLengthError(Exception):
    """Raised when the server rejects a request due to exceeding the context window."""


class InferenceTimeoutError(Exception):
    """Raised when a request to the inference backend does not respond in time."""


_RETRYABLE_DISCONNECT = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
    TimeoutError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
)


def _is_retryable_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE_DISCONNECT):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, _RETRYABLE_DISCONNECT):
            return True
        text = str(reason or exc).lower()
        return any(
            needle in text
            for needle in (
                "remote end closed",
                "connection reset",
                "broken pipe",
                "connection refused",
            )
        )
    return False


def _normalize_base_url(base_url: str) -> str:
    """Ensure OpenAI-compatible base URLs have a scheme (and /v1 for bare Ollama hosts)."""
    value = (base_url or "").strip().rstrip("/")
    if not value:
        return value
    if "://" not in value:
        value = f"http://{value}"
    # Bare Ollama host/port without the OpenAI-compat prefix.
    if value.rstrip("/").endswith(":11434"):
        value = f"{value}/v1"
    return value.rstrip("/")


class BaseAgent:
    """Shared HTTP communication and tool-execution logic for all AutoSE agents."""

    _MAX_TOOL_OUTPUT: int = int(os.environ.get("AUTOSE_MAX_TOOL_OUTPUT") or 4000)
    # Number of tool-call rounds to retain in history (system is always kept).
    _MAX_HISTORY_ROUNDS: int = 20
    # Safety-only round ceiling. The real stop for eval is AUTOSE_DEADLINE_UNIX
    # (DeepSWE 90-minute agent wall). Do not use this as a small per-stage budget.
    _MAX_TOOL_ROUNDS: int = 100_000
    _ENABLE_EVIDENCE_COMPACTION: bool = False
    _MAX_EVIDENCE_NOTES: int = 6
    _MAX_EVIDENCE_DETAIL_CHARS: int = 180
    _CONTEXT_SAFETY_FACTOR: float = 1.10
    # Per-request socket timeout (seconds) for calls to the inference backend.
    # Generous enough for slow local/self-hosted models (large context, weak
    # hardware) while still guaranteeing a stuck request eventually raises
    # InferenceTimeoutError instead of hanging the process forever.
    _REQUEST_TIMEOUT: int | None = None

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        workspace_root: str = ".",
        temperature: float = 0.2,
        context_limit: int | None = None,
        reserved_output_tokens: int = 8192,
        request_extras: dict | None = None,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._api_key = api_key
        self._model = model
        self._workspace = Path(workspace_root).resolve()
        self._temperature = temperature
        self._context_limit = context_limit if context_limit and context_limit > 0 else None
        self._reserved_output_tokens = max(0, reserved_output_tokens)
        self._request_extras = dict(request_extras or {})
        self._session_id = os.environ.get("AUTOSE_SESSION_ID") or str(uuid.uuid4())
        self.context_metrics: list[dict] = []
        self._recent_tool_sigs: list[str] = []
        # Subclasses must set self._tools to their TOOLS dict.
        self._tools: dict = {}

    @staticmethod
    def _deadline_reached() -> bool:
        raw = os.environ.get("AUTOSE_DEADLINE_UNIX")
        if not raw:
            return False
        try:
            return time.time() >= float(raw)
        except ValueError:
            return False

    @staticmethod
    def _eval_deadline_active() -> bool:
        return bool(os.environ.get("AUTOSE_DEADLINE_UNIX"))

    @staticmethod
    def _tool_call_names(tool_calls: list | None) -> set[str]:
        names: set[str] = set()
        for call in tool_calls or []:
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            names.add(str(function.get("name") or call.get("name") or ""))
        return names

    def _push_text_and_nudge(
        self,
        messages: list[dict],
        message: dict,
        last_content: str,
        nudge: str,
    ) -> None:
        text = str(message.get("content") or last_content or "")
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": nudge})

    # ------------------------------------------------------------------

    def _prune_messages(self, messages: list[dict]) -> list[dict]:
        """Drop old tool-call rounds to keep the context window manageable.

        Always preserves:
        - messages[0]: system prompt
        - The last ``_MAX_HISTORY_ROUNDS`` conversation rounds
        """
        if len(messages) <= 1:
            return messages

        head = messages[:1]  # system
        tail = messages[1:]  # all subsequent messages

        # Group tail into rounds: each round starts with a user or assistant message.
        rounds: list[list[dict]] = []
        current: list[dict] = []
        for msg in tail:
            if msg["role"] in ("assistant", "user"):
                if current:
                    rounds.append(current)
                current = [msg]
            else:
                current.append(msg)
        if current:
            rounds.append(current)

        if len(rounds) <= self._MAX_HISTORY_ROUNDS:
            return messages

        dropped = len(rounds) - self._MAX_HISTORY_ROUNDS
        kept = rounds[-self._MAX_HISTORY_ROUNDS :]
        notice = (
            f"[{dropped} earlier conversation round(s) were dropped to stay "
            "within the context window. Rely on information gathered in the "
            "remaining rounds.]"
        )
        return self._merge_system_notice(head, notice) + [
            msg for r in kept for msg in r
        ]

    @staticmethod
    def _is_context_http_error(code: int, body_text: str, reason: str = "") -> bool:
        if code not in (400, 413):
            return False
        blob = f"{body_text} {reason}".lower()
        if any(
            needle in blob
            for needle in (
                "context",
                "token",
                "length",
                "exceed",
                "maximum",
                "too long",
            )
        ):
            return True
        return code == 400 and not (body_text or "").strip()

    @staticmethod
    def _merge_system_notice(system: list[dict], notice_content: str) -> list[dict]:
        """Fold a compaction note into the leading system turn.

        Qwen/vLLM reject any ``role=system`` after messages[0] with
        ``System message must be at the beginning``.
        """
        if not system:
            return [{"role": "user", "content": notice_content}]
        first = dict(system[0])
        existing = str(first.get("content") or "")
        first["role"] = "system"
        first["content"] = (
            f"{existing}\n\n{notice_content}" if existing else notice_content
        )
        return [first]

    @staticmethod
    def _coerce_single_leading_system(messages: list[dict]) -> list[dict]:
        """Keep at most one system message, and only as messages[0]."""
        if not messages:
            return messages
        leading: list[str] = []
        rest_start = 0
        for index, message in enumerate(messages):
            if message.get("role") == "system":
                leading.append(str(message.get("content") or ""))
                rest_start = index + 1
                continue
            break
        rest: list[dict] = []
        for message in messages[rest_start:]:
            if message.get("role") == "system":
                rest.append(
                    {
                        **message,
                        "role": "user",
                        "content": str(message.get("content") or ""),
                    }
                )
            else:
                rest.append(message)
        if not leading:
            return rest
        first = dict(messages[0])
        first["role"] = "system"
        first["content"] = "\n\n".join(part for part in leading if part)
        return [first] + rest

    @staticmethod
    def _drop_oldest_tool_group(messages: list[dict]) -> list[dict]:
        """Drop the oldest assistant+tool group so a 400 can be retried smaller."""
        start = next(
            (i for i, message in enumerate(messages) if message.get("role") == "assistant"),
            None,
        )
        if start is None:
            return messages
        end = start + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        if end >= len(messages):
            return messages
        return messages[:start] + messages[end:]

    @staticmethod
    def _estimate_tokens(value: object) -> int:
        """Return a conservative tokenizer-independent size estimate."""
        return max(1, (len(json.dumps(value, ensure_ascii=False)) + 2) // 3)

    def _prepare_messages(
        self, messages: list[dict], tools: list | None = None
    ) -> list[dict]:
        """Build one bounded request history for sync and streaming calls.

        The system prompt and latest user request are mandatory. Older session
        context is discarded before recent, complete assistant/tool groups.
        """
        original_count = len(messages)
        tool_tokens = self._estimate_tokens(tools) if tools else 0
        if self._context_limit is None:
            prepared = self._coerce_single_leading_system(
                self._prune_messages(messages)
            )
            self._record_context_metrics(
                prepared, original_count, tool_tokens, context_limit=None
            )
            return prepared
        messages = list(messages)
        if not messages:
            return messages

        input_budget = self._context_limit - self._reserved_output_tokens
        raw_input_budget = math.floor(input_budget / self._CONTEXT_SAFETY_FACTOR)
        message_budget = raw_input_budget - tool_tokens
        if message_budget <= 0:
            self._record_context_metrics(
                messages, original_count, tool_tokens, context_limit=self._context_limit
            )
            raise ContextLengthError(
                "Tool schemas, safety margin, and reserved output consume the "
                "configured context window."
            )
        if self._estimate_tokens(messages) <= message_budget:
            prepared = self._coerce_single_leading_system(messages)
            self._record_context_metrics(
                prepared, original_count, tool_tokens, context_limit=self._context_limit
            )
            return prepared

        system = messages[:1] if messages[0].get("role") == "system" else []
        latest_user_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "user"
            ),
            None,
        )
        if latest_user_index is None:
            self._record_context_metrics(
                messages, original_count, tool_tokens, context_limit=self._context_limit
            )
            raise ContextLengthError(
                "The request exceeds the configured context window and has no user turn to preserve."
            )

        current_user = messages[latest_user_index]
        mandatory = system + [current_user]
        if self._estimate_tokens(mandatory) > message_budget:
            self._record_context_metrics(
                mandatory, original_count, tool_tokens, context_limit=self._context_limit
            )
            raise ContextLengthError(
                "The system prompt and current stage input exceed the configured context window."
            )

        groups: list[list[dict]] = []
        current: list[dict] = []
        for message in messages[latest_user_index + 1 :]:
            if message.get("role") == "assistant":
                if current:
                    groups.append(current)
                current = [message]
                continue
            if current:
                current.append(message)
        if current:
            groups.append(current)

        kept: list[list[dict]] = []
        for group in reversed(groups):
            candidate = mandatory + [message for item in [group] + kept for message in item]
            if self._estimate_tokens(candidate) > message_budget:
                break
            kept.insert(0, group)

        dropped_groups = groups[: len(groups) - len(kept)]
        notice_prefix = (
            "[Earlier session context and tool exploration were omitted to fit "
            "the configured context window. Canonical stage inputs below remain authoritative.]"
        )
        evidence_lines = (
            self._evidence_lines(dropped_groups)
            if self._ENABLE_EVIDENCE_COMPACTION
            else []
        )
        evidence_note = self._fit_evidence_note(
            evidence_lines,
            system=system,
            current_user=current_user,
            kept=kept,
            message_budget=message_budget,
            notice_prefix=notice_prefix,
        )
        notice_content = notice_prefix
        if evidence_note:
            notice_content += "\n\nEarlier tool evidence (derived, bounded):\n" + evidence_note
        result = self._merge_system_notice(system, notice_content) + [
            current_user
        ] + [message for group in kept for message in group]
        if self._estimate_tokens(result) > message_budget:
            result = mandatory + [message for group in kept for message in group]
        result = self._coerce_single_leading_system(result)
        self._record_context_metrics(
            result,
            original_count,
            tool_tokens,
            context_limit=self._context_limit,
            evidence_note_count=len(evidence_note.splitlines()) if evidence_note else 0,
            estimated_evidence_tokens=self._estimate_tokens(evidence_note) if evidence_note else 0,
        )
        return result

    def _evidence_lines(self, groups: list[list[dict]]) -> list[str]:
        """Derive small, non-protocol notes from discarded tool-call groups."""
        lines: list[str] = []
        for group in reversed(groups):
            results = {
                message.get("tool_call_id"): str(message.get("content") or "")
                for message in group
                if message.get("role") == "tool"
            }
            for message in reversed(group):
                if message.get("role") != "assistant":
                    continue
                for call in reversed(message.get("tool_calls") or []):
                    function = call.get("function", {})
                    name = str(function.get("name", "tool"))
                    raw_arguments = function.get("arguments", "")
                    try:
                        arguments = json.loads(raw_arguments) if raw_arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                    descriptor = self._evidence_descriptor(arguments)
                    result = " ".join(results.get(call.get("id"), "no result").split())
                    result = result[: self._MAX_EVIDENCE_DETAIL_CHARS]
                    status = "failed" if result.lower().startswith("error") else "observed"
                    lines.append(f"- {name}{descriptor} — {status}: {result}")
                    if len(lines) >= self._MAX_EVIDENCE_NOTES:
                        return list(reversed(lines))
        return list(reversed(lines))

    @staticmethod
    def _evidence_descriptor(arguments: object) -> str:
        if not isinstance(arguments, dict):
            return ""
        for key in ("path", "file_path", "command", "query", "pattern"):
            value = arguments.get(key)
            if value is not None:
                compact = " ".join(str(value).split())[:120]
                return f"({key}={compact})"
        return ""

    def _fit_evidence_note(
        self,
        lines: list[str],
        *,
        system: list[dict],
        current_user: dict,
        kept: list[list[dict]],
        message_budget: int,
        notice_prefix: str,
    ) -> str:
        """Keep only evidence lines that fit alongside mandatory and recent context."""
        selected: list[str] = []
        recent = [message for group in kept for message in group]
        for line in lines:
            candidate_lines = selected + [line]
            candidate = self._merge_system_notice(
                system,
                notice_prefix
                + "\n\nEarlier tool evidence (derived, bounded):\n"
                + "\n".join(candidate_lines),
            ) + [current_user] + recent
            if self._estimate_tokens(candidate) > message_budget:
                break
            selected = candidate_lines
        return "\n".join(selected)

    def _record_context_metrics(
        self,
        messages: list[dict],
        original_count: int,
        tool_tokens: int,
        *,
        context_limit: int | None,
        evidence_note_count: int = 0,
        estimated_evidence_tokens: int = 0,
    ) -> None:
        estimated_input_tokens = self._estimate_tokens(messages) + tool_tokens
        self.context_metrics.append(
            {
                "estimated_input_tokens": estimated_input_tokens,
                "safety_adjusted_input_tokens": math.ceil(
                    estimated_input_tokens * self._CONTEXT_SAFETY_FACTOR
                ),
                "context_safety_factor": self._CONTEXT_SAFETY_FACTOR,
                "context_limit": context_limit,
                "input_token_budget": (
                    context_limit - self._reserved_output_tokens
                    if context_limit is not None
                    else None
                ),
                "reserved_output_tokens": self._reserved_output_tokens,
                "estimated_tool_schema_tokens": tool_tokens,
                "original_message_count": original_count,
                "sent_message_count": len(messages),
                "pruned_message_count": max(0, original_count - len(messages)),
                "evidence_note_count": evidence_note_count,
                "estimated_evidence_tokens": estimated_evidence_tokens,
            }
        )

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            # Cloudflare 1010s the default Python-urllib User-Agent.
            "User-Agent": "autose/1.0",
            # OpenCode Go routes on a stable per-conversation session id.
            "x-opencode-session": self._session_id,
        }
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _apply_output_limit(self, body: dict) -> None:
        """Always send max_tokens that still fits in the context window.

        reserved_output_tokens=0 used to skip this field. vLLM then assumed a
        large default completion budget, so prompt + default overflowed 262k
        and returned HTTP 400.
        """
        prompt_est = self._estimate_tokens(body.get("messages"))
        if body.get("tools"):
            prompt_est += self._estimate_tokens(body.get("tools"))
        if self._context_limit is None:
            if self._reserved_output_tokens > 0:
                body["max_tokens"] = self._reserved_output_tokens
            return
        remaining = self._context_limit - prompt_est - 64
        cap = self._reserved_output_tokens if self._reserved_output_tokens > 0 else 16384
        body["max_tokens"] = max(256, min(cap, remaining))

    def _trace(
        self, body: dict, prepared: list[dict], data: dict, original_count: int, started: float
    ) -> None:
        """DEBUG: append one request/response record to $AUTOSE_TRACE_PATH."""
        path = os.environ.get("AUTOSE_TRACE_PATH")
        if not path:
            return
        try:
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            native = message.get("tool_calls") or []
            parsed = self._assistant_tool_calls(message) or []
            record = {
                "agent": type(self).__name__,
                "t_start": started,
                "t_end": time.time(),
                "original_messages": original_count,
                "sent_messages": len(prepared),
                "sent_roles": [m.get("role") for m in prepared],
                "dropped_notice": any(
                    "omitted to fit" in str(m.get("content") or "")
                    or "were dropped" in str(m.get("content") or "")
                    for m in prepared[:1]
                ),
                "est_prompt_tokens": self._estimate_tokens(prepared),
                "max_tokens": body.get("max_tokens"),
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"),
                "content": message.get("content"),
                "reasoning": message.get("reasoning_content") or message.get("reasoning"),
                "native_tool_calls": [
                    (c.get("function") or {}).get("name") for c in native
                ],
                "parsed_tool_calls": [
                    {
                        "name": (c.get("function") or {}).get("name"),
                        "args": str((c.get("function") or {}).get("arguments"))[:300],
                        "fallback": not native,
                    }
                    for c in parsed
                ],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    def _merge_request_extras(self, body: dict) -> None:
        extras = dict(self._request_extras)
        for key in ("messages", "model", "tools", "stream"):
            extras.pop(key, None)
        body.update(extras)

    def _call_sync(
        self,
        messages: list[dict],
        tools: list | None = None,
        tool_choice: str | None = None,
    ) -> dict:
        """Non-streaming call, used for tool-calling rounds."""
        work = list(messages)
        last_context: str = ""
        for _shrink in range(8):
            prepared = self._prepare_messages(work, tools)
            body: dict = {
                "model": self._model,
                "messages": prepared,
                "temperature": self._temperature,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = tool_choice or "auto"
            self._merge_request_extras(body)
            self._apply_output_limit(body)
            payload = json.dumps(body).encode("utf-8")
            last_exc: BaseException | None = None
            for attempt in range(4):
                req = urllib.request.Request(
                    f"{self._base_url}/chat/completions",
                    data=payload,
                    headers=self._headers(),
                    method="POST",
                )
                started = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=self._REQUEST_TIMEOUT) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        self._trace(body, prepared, data, len(messages), started)
                        return data
                except urllib.error.HTTPError as exc:
                    body_text = ""
                    try:
                        body_text = exc.read().decode("utf-8")
                    except Exception:
                        pass
                    if self._is_context_http_error(exc.code, body_text, str(exc.reason or "")):
                        last_context = body_text.strip() or str(exc.reason or "HTTP 400")
                        break
                    raise RuntimeError(
                        f"HTTP {exc.code}: {body_text.strip() or exc.reason}"
                    ) from exc
                except (urllib.error.URLError, TimeoutError, *_RETRYABLE_DISCONNECT) as exc:
                    last_exc = exc
                    if attempt < 3 and _is_retryable_disconnect(exc):
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise InferenceTimeoutError(
                        f"Could not reach the inference backend at {self._base_url}: {exc}"
                    ) from exc
            else:
                raise InferenceTimeoutError(
                    f"Could not reach the inference backend at {self._base_url}: {last_exc}"
                ) from last_exc
            shrunk = self._drop_oldest_tool_group(work)
            if shrunk == work:
                raise ContextLengthError(last_context or "context window exceeded")
            work = shrunk
        raise ContextLengthError(last_context or "context window exceeded")

    def _call_stream(
        self, messages: list[dict], tools: list | None = None
    ) -> Iterator[str]:
        """Streaming call for the final answer. Falls back to non-streaming on error."""
        messages = self._prepare_messages(messages, tools)
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        self._merge_request_extras(body)
        self._apply_output_limit(body)
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._REQUEST_TIMEOUT) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").rstrip("\n\r")
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, *_RETRYABLE_DISCONNECT):
            response = self._call_sync(messages)
            content = response["choices"][0]["message"].get("content") or ""
            if content:
                yield content

    def _assistant_text(self, message: dict) -> str:
        parts = [
            message.get("content") or "",
            message.get("reasoning_content") or "",
            message.get("reasoning") or "",
        ]
        return "\n".join(part for part in parts if part)

    def _assistant_tool_calls(self, message: dict) -> list | None:
        """Native tool_calls, else text fallback from content or reasoning."""
        tool_calls = message.get("tool_calls") or None
        if tool_calls:
            return tool_calls
        return self._parse_text_tool_calls(self._assistant_text(message))

    def _parse_text_tool_calls(self, content: str | None) -> list[dict] | None:
        """Fallback parser for models that emit tool calls as text instead of the
        tool_calls API field.

        Handles ``call:tool_name{key:value}``, ``<tool_call>{...}</tool_call>``,
        and ``<function=name><parameter=key>value</parameter></function>``.
        """
        blob = content or ""
        parsed = self._parse_xml_tool_calls(blob)
        if parsed:
            return parsed
        match = re.search(r"call:(\w+)\{([^}]*)\}", blob)
        if not match:
            return None
        tool_name = match.group(1)
        if tool_name not in self._tools:
            return None
        args_str = match.group(2).strip()
        args: dict = {}
        # Split on commas that precede a bare word followed by a colon so that
        # values containing commas (e.g. shell commands) are kept intact.
        for part in re.split(r",\s*(?=\w[\w\s]*:)", args_str):
            part = part.strip()
            colon = part.find(":")
            if colon < 0:
                continue
            key = part[:colon].strip()
            value = part[colon + 1 :].strip()
            if key:
                args[key] = value
        if not args:
            return None
        return [self._text_tool_call(tool_name, args, 0)]

    def _parse_xml_tool_calls(self, blob: str) -> list[dict] | None:
        calls: list[dict] = []
        for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", blob, re.DOTALL):
            inner = match.group(1).strip()
            if inner.startswith("{"):
                try:
                    payload = json.loads(inner)
                except json.JSONDecodeError:
                    continue
                name = payload.get("name") or payload.get("function")
                args = payload.get("arguments") or payload.get("parameters") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                if name in self._tools and isinstance(args, dict):
                    calls.append(self._text_tool_call(name, args, len(calls)))
                continue
            fn_match = re.search(r"<function=(\w+)>(.*)</function>", inner, re.DOTALL)
            if fn_match and fn_match.group(1) in self._tools:
                calls.append(
                    self._text_tool_call(
                        fn_match.group(1),
                        self._xml_parameters(fn_match.group(2)),
                        len(calls),
                    )
                )
        for match in re.finditer(r"<function=(\w+)>(.*?)</function>", blob, re.DOTALL):
            name = match.group(1)
            if name not in self._tools:
                continue
            if any(
                call["function"]["name"] == name
                and call["function"]["arguments"]
                == json.dumps(self._xml_parameters(match.group(2)))
                for call in calls
            ):
                continue
            calls.append(
                self._text_tool_call(name, self._xml_parameters(match.group(2)), len(calls))
            )
        return calls or None

    @staticmethod
    def _xml_parameters(inner: str) -> dict:
        args: dict = {}
        for match in re.finditer(
            r"<parameter=([^>]+)>(.*?)</parameter>", inner, re.DOTALL
        ):
            args[match.group(1).strip()] = match.group(2)
        return args

    def _text_tool_call(self, name: str, args: dict, index: int) -> dict:
        return {
            "id": f"text_fallback_{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def _yield_final_text(
        self, messages: list[dict], message: dict, last_content: str
    ) -> Iterator[str]:
        """Prefer the current turn's text. Do not restream with tools enabled."""
        content = str(message.get("content") or "").strip()
        if content:
            yield content
            return
        produced = False
        for chunk in self._call_stream(messages, tools=None):
            if chunk:
                produced = True
                yield chunk
        if produced:
            return
        if str(last_content or "").strip():
            yield last_content

    def _execute_tool(self, tool_call: dict) -> str:
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        raw_args = function.get("arguments", "{}")
        try:
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as exc:
            return f"Error: could not parse tool arguments: {exc}"
        if not isinstance(args, dict):
            return f"Error: tool arguments must be an object, got {type(args).__name__}"
        if name not in self._tools:
            return f"Error: unknown tool '{name}'"
        sig = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if (
            len(self._recent_tool_sigs) >= 2
            and self._recent_tool_sigs[-1] == sig
            and self._recent_tool_sigs[-2] == sig
        ):
            return (
                "Error: identical tool call repeated three times. Stop rereading "
                "the same slice; try a different path, query, or write the change."
            )
        self._recent_tool_sigs.append(sig)
        if len(self._recent_tool_sigs) > 24:
            self._recent_tool_sigs = self._recent_tool_sigs[-24:]
        try:
            result = self._tools[name](workspace_root=str(self._workspace), **args)
        except TypeError as exc:
            return f"Error: invalid arguments for tool '{name}': {exc}"
        if len(result) > self._MAX_TOOL_OUTPUT:
            result = (
                result[: self._MAX_TOOL_OUTPUT]
                + f"\n\n[Output truncated: {len(result)} chars total, showing first {self._MAX_TOOL_OUTPUT}. Use start_line/end_line or a narrower search to read more.]"
            )
        return result
