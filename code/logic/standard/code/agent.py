import json
from pathlib import Path

from common.agent import BaseAgent, ContextLengthError

from .tools import TOOLS, TOOLS_SCHEMA

_PROMPTS_FILE = Path(__file__).parent.parent.parent / "prompts.json"

with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)["standard_code_agent"]


class CodeAgent(BaseAgent):
    """
    Implements a plan by making targeted file changes.
    Runs a tool-calling loop until all changes are applied, then returns a summary string.
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

    def run(self, prompt: str, plan: str) -> str:
        """Execute the plan against the workspace and return a summary of all changes made."""
        system = _PROMPTS["system"].format(workspace_root=str(self._workspace))
        user_content = (
            f"## Original task\n{prompt}\n\n"
            f"## Implementation plan\n{plan}\n\n"
            "Execute the plan above. Read any files you need for context, then apply all changes."
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

        last_content = ""
        for _round in range(self._MAX_TOOL_ROUNDS):
            try:
                response = self._call_sync(messages, tools=TOOLS_SCHEMA)
            except ContextLengthError:
                return "Error: context window exceeded while coding. The plan may be too large."

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
                return message.get("content", "")

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
        return last_content
