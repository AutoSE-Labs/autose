import json
from collections.abc import Iterator
from pathlib import Path

from common.agent import BaseAgent, ContextLengthError

from .tools import TOOLS, TOOLS_SCHEMA

_PROMPTS_FILE = Path(__file__).parent.parent.parent / "prompts.json"

with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)["standard_plan_agent"]


class PlanAgent(BaseAgent):
    """
    Explores the codebase and produces a detailed implementation plan.
    Runs a tool-calling loop until the model is ready, then streams the plan.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        workspace_root: str = ".",
    ) -> None:
        super().__init__(base_url, api_key, model, workspace_root)
        self._tools = TOOLS

    def run(
        self,
        prompt: str,
        previous_plan: str = "",
        feedback: str = "",
        session_context: list[dict] | None = None,
    ) -> Iterator[str]:
        """Explore the workspace and stream a structured implementation plan.

        If *previous_plan* and *feedback* are both provided the agent will
        revise the plan based on the user's requested changes.

        *session_context* is an optional list of messages (produced by
        ``MemoryManager.build_context_messages()``) injected after the system
        prompt to provide prior session history.
        """
        system = _PROMPTS["system"].format(workspace_root=str(self._workspace))

        if previous_plan and feedback:
            user_content = (
                f"{prompt}\n\n"
                f"You previously generated this plan:\n{previous_plan}\n\n"
                f"The user requested the following changes: {feedback}\n\n"
                f"Please revise the plan accordingly."
            )
        else:
            user_content = prompt

        messages: list[dict] = [{"role": "system", "content": system}]
        if session_context:
            messages.extend(session_context)
        messages.append({"role": "user", "content": user_content})

        last_content = ""
        for _round in range(self._MAX_TOOL_ROUNDS):
            try:
                response = self._call_sync(messages, tools=TOOLS_SCHEMA)
            except ContextLengthError:
                yield "Error: context window exceeded while planning. Try a more specific task."
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
                yield from self._call_stream(messages, tools=TOOLS_SCHEMA)
                return

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
