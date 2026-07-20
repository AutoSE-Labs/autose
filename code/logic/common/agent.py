import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path


class ContextLengthError(Exception):
    """Raised when the server rejects a request due to exceeding the context window."""


class InferenceTimeoutError(Exception):
    """Raised when a request to the inference backend does not respond in time."""


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

    _MAX_TOOL_OUTPUT: int = 4000
    # Number of tool-call rounds to retain in history (system is always kept).
    _MAX_HISTORY_ROUNDS: int = 20
    # Hard ceiling on tool-calling rounds within a single agent.run() call.
    # Prevents a model stuck in a read/search loop from hanging a stage
    # indefinitely — after this many rounds the agent forces a final answer.
    _MAX_TOOL_ROUNDS: int = 30
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
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._api_key = api_key
        self._model = model
        self._workspace = Path(workspace_root).resolve()
        self._temperature = temperature
        self._context_limit = context_limit if context_limit and context_limit > 0 else None
        self._reserved_output_tokens = max(0, reserved_output_tokens)
        self.context_metrics: list[dict] = []
        # Subclasses must set self._tools to their TOOLS dict.
        self._tools: dict = {}

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
        notice: list[dict] = [
            {
                "role": "system",
                "content": (
                    f"[{dropped} earlier conversation round(s) were dropped to stay "
                    "within the context window. Rely on information gathered in the "
                    "remaining rounds.]"
                ),
            }
        ]
        return head + notice + [msg for r in kept for msg in r]

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
            prepared = self._prune_messages(messages)
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
            self._record_context_metrics(
                messages, original_count, tool_tokens, context_limit=self._context_limit
            )
            return messages

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
        notice = {
            "role": "system",
            "content": notice_content,
        }
        result = system + [notice, current_user] + [
            message for group in kept for message in group
        ]
        if self._estimate_tokens(result) > message_budget:
            result = mandatory + [message for group in kept for message in group]
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
                message.get("tool_call_id"): str(message.get("content", ""))
                for message in group
                if message.get("role") == "tool"
            }
            for message in reversed(group):
                if message.get("role") != "assistant":
                    continue
                for call in reversed(message.get("tool_calls", [])):
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
            candidate = system + [
                {
                    "role": "system",
                    "content": (
                        notice_prefix
                        + "\n\nEarlier tool evidence (derived, bounded):\n"
                        + "\n".join(candidate_lines)
                    ),
                },
                current_user,
            ] + recent
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
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _apply_output_limit(self, body: dict) -> None:
        """Cap generation when a configured context budget reserves output space."""
        if self._context_limit is not None and self._reserved_output_tokens > 0:
            body["max_tokens"] = self._reserved_output_tokens

    def _call_sync(self, messages: list[dict], tools: list | None = None) -> dict:
        """Non-streaming call, used for tool-calling rounds."""
        messages = self._prepare_messages(messages, tools)
        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if tools:
            body["tools"] = tools
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
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8")
            except Exception:
                pass
            lower = body_text.lower()
            if any(
                k in lower for k in ("context", "token", "length", "exceed", "maximum")
            ):
                raise ContextLengthError(body_text) from exc
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InferenceTimeoutError(
                f"Could not reach the inference backend at {self._base_url}: {exc}"
            ) from exc

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
        except urllib.error.HTTPError:
            response = self._call_sync(messages)
            content = response["choices"][0]["message"].get("content") or ""
            if content:
                yield content
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InferenceTimeoutError(
                f"Could not reach the inference backend at {self._base_url}: {exc}"
            ) from exc

    def _parse_text_tool_calls(self, content: str) -> list[dict] | None:
        """Fallback parser for models that emit tool calls as text instead of the
        tool_calls API field.  Handles the format::

            call:tool_name{key:value, key:value, ...}

        Returns a list compatible with the ``tool_calls`` API shape, or None if
        no recognised pattern is found.
        """
        match = re.search(r"call:(\w+)\{([^}]*)\}", content)
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
        return [
            {
                "id": "text_fallback_0",
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(args)},
            }
        ]

    def _execute_tool(self, tool_call: dict) -> str:
        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError as exc:
            return f"Error: could not parse tool arguments: {exc}"
        if name not in self._tools:
            return f"Error: unknown tool '{name}'"
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
