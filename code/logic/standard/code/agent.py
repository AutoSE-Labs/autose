import json
from pathlib import Path

from common.agent import BaseAgent, ContextLengthError

from .tools import TOOLS, TOOLS_SCHEMA

_EDIT_TOOLS = frozenset({"write_file", "edit_file"})
_NO_WRITE_NUDGES = 8
_POST_WRITE_NUDGES = 3
_NO_WRITE_NUDGE = (
    "A text-only summary with no write_file or edit_file leaves the repository "
    "unchanged and is a failed turn. Call write_file or edit_file now."
)
_KEEP_CODING_NUDGE = (
    "Do not stop yet. If anything is still unfinished, keep using tools. "
    "A text-only reply ends coding and you cannot come back after the test stage."
)

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
        context_limit: int | None = None,
        reserved_output_tokens: int = 8192,
        request_extras: dict | None = None,
    ) -> None:
        super().__init__(
            base_url,
            api_key,
            model,
            workspace_root,
            context_limit=context_limit,
            reserved_output_tokens=reserved_output_tokens,
            request_extras=request_extras,
        )
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
        file_edits = 0
        text_nudges = 0
        for _round in range(self._MAX_TOOL_ROUNDS):
            if self._deadline_reached():
                return last_content
            try:
                response = self._call_sync(messages, tools=TOOLS_SCHEMA)
            except ContextLengthError:
                shrunk = self._drop_oldest_tool_group(messages)
                if shrunk == messages:
                    return last_content or (
                        "Error: context window exceeded while coding. The plan may be too large."
                    )
                messages = shrunk
                continue

            choice = response["choices"][0]
            message = choice["message"]
            if message.get("content"):
                last_content = message["content"]
            tool_calls = self._assistant_tool_calls(message)
            if tool_calls and not message.get("tool_calls"):
                message = {**message, "content": None, "tool_calls": tool_calls}

            if not tool_calls:
                if choice.get("finish_reason") == "length":
                    messages.append({"role": "assistant", "content": last_content or ""})
                    messages.append(
                        {
                            "role": "user",
                            "content": "Your last reply was cut off. Continue.",
                        }
                    )
                    continue
                if file_edits == 0 and text_nudges < _NO_WRITE_NUDGES:
                    text_nudges += 1
                    self._push_text_and_nudge(
                        messages, message, last_content, _NO_WRITE_NUDGE
                    )
                    continue
                if (
                    file_edits > 0
                    and self._eval_deadline_active()
                    and text_nudges < _POST_WRITE_NUDGES
                ):
                    text_nudges += 1
                    self._push_text_and_nudge(
                        messages, message, last_content, _KEEP_CODING_NUDGE
                    )
                    continue
                return message.get("content") or last_content or ""

            names = self._tool_call_names(tool_calls)
            if names & _EDIT_TOOLS:
                file_edits += 1
                text_nudges = 0
            messages.append(message)
            for tc in tool_calls:
                result = self._execute_tool(tc)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or "tool_call",
                        "content": result,
                    }
                )

        return last_content
