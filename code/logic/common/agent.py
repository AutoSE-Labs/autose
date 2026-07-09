import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path


class ContextLengthError(Exception):
    """Raised when the server rejects a request due to exceeding the context window."""


class BaseAgent:
    """Shared HTTP communication and tool-execution logic for all AutoSE agents."""

    _MAX_TOOL_OUTPUT: int = 4000
    # Number of tool-call rounds to retain in history (system + user are always kept).
    _MAX_HISTORY_ROUNDS: int = 8

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        workspace_root: str = ".",
        temperature: float = 0.2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._workspace = Path(workspace_root).resolve()
        self._temperature = temperature
        # Subclasses must set self._tools to their TOOLS dict.
        self._tools: dict = {}

    # ------------------------------------------------------------------

    def _prune_messages(self, messages: list[dict]) -> list[dict]:
        """Drop old tool-call rounds to keep the context window manageable.

        Always preserves:
        - messages[0]: system prompt
        - messages[1]: original user message
        - The last ``_MAX_HISTORY_ROUNDS`` assistant+tool rounds
        """
        if len(messages) <= 2:
            return messages

        head = messages[:2]  # system + user
        tail = messages[2:]  # all subsequent assistant/tool messages

        # Group tail into rounds: each round starts with an assistant message.
        rounds: list[list[dict]] = []
        current: list[dict] = []
        for msg in tail:
            if msg["role"] == "assistant":
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
                    f"[{dropped} earlier tool-call round(s) were dropped to stay "
                    "within the context window. Rely on information gathered in the "
                    "remaining rounds.]"
                ),
            }
        ]
        return head + notice + [msg for r in kept for msg in r]

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _call_sync(self, messages: list[dict], tools: list | None = None) -> dict:
        """Non-streaming call, used for tool-calling rounds."""
        messages = self._prune_messages(messages)
        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if tools:
            body["tools"] = tools
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
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

    def _call_stream(
        self, messages: list[dict], tools: list | None = None
    ) -> Iterator[str]:
        """Streaming call for the final answer. Falls back to non-streaming on error."""
        body = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
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
            yield response["choices"][0]["message"]["content"]

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
            value = part[colon + 1:].strip()
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
        result = self._tools[name](workspace_root=str(self._workspace), **args)
        if len(result) > self._MAX_TOOL_OUTPUT:
            result = (
                result[: self._MAX_TOOL_OUTPUT]
                + f"\n\n[Output truncated: {len(result)} chars total, showing first {self._MAX_TOOL_OUTPUT}. Use start_line/end_line or a narrower search to read more.]"
            )
        return result
