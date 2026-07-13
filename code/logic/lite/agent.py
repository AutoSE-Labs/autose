import json
from collections.abc import Iterator
from pathlib import Path

from common.agent import BaseAgent, ContextLengthError

from .tools import TOOLS, TOOLS_SCHEMA

_PROMPTS_FILE = Path(__file__).parent.parent / "prompts.json"

with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)["lite_agent"]


class LiteAgent(BaseAgent):
    """
    Answers a user question using an agentic tool-calling loop.
    The model receives the prompt and tool schemas, calls tools as needed,
    then streams the final answer.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        workspace_root: str = ".",
    ) -> None:
        super().__init__(base_url, api_key, model, workspace_root, temperature=0.3)
        self._tools = TOOLS

    def run(
        self, prompt: str, session_context: list[dict] | None = None
    ) -> Iterator[str]:
        """Agentic loop: call tools until the model is ready to answer, then stream.

        *session_context* is an optional list of messages (produced by
        ``MemoryManager.build_context_messages()``) injected between the system
        prompt and the current user message to give the agent session history.
        """
        system = _PROMPTS["system"].format(workspace_root=str(self._workspace))
        messages: list[dict] = [{"role": "system", "content": system}]
        if session_context:
            messages.extend(session_context)
        messages.append({"role": "user", "content": prompt})

        last_content = ""
        for _round in range(self._MAX_TOOL_ROUNDS):
            try:
                response = self._call_sync(messages, tools=TOOLS_SCHEMA)
            except ContextLengthError:
                yield "Error: the accumulated context exceeds the model's context window. Try a more specific question."
                return
            choice = response["choices"][0]
            message = choice["message"]
            if message.get("content"):
                last_content = message["content"]
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                tool_calls = self._parse_text_tool_calls(message.get("content", ""))
                if tool_calls:
                    message = {**message, "content": None, "tool_calls": tool_calls}

            if not tool_calls:
                # Model is done with tools — stream the final answer
                yield from self._call_stream(messages, tools=TOOLS_SCHEMA)
                return

            # Append assistant turn and execute each tool call
            messages.append(message)
            for tc in tool_calls:
                result = self._execute_tool(tc)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    }
                )

        # Tool-call round budget exhausted — surface whatever the model has
        # already drafted rather than issuing another (possibly slow) call.
        yield last_content
