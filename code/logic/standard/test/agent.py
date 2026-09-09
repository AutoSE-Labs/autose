import json
from collections.abc import Iterator
from pathlib import Path

from common.agent import BaseAgent, ContextLengthError

from .tools import TOOLS, TOOLS_SCHEMA

_KEEP_TESTING_NUDGE = (
    "Do not stop. Wall-clock remains. Do not treat tests you wrote as the "
    "evaluator. Run the project's existing test suite and keep fixing "
    "production code with tools."
)

_PROMPTS_FILE = Path(__file__).parent.parent.parent / "prompts.json"

with open(_PROMPTS_FILE, "r", encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)["standard_test_agent"]


class TestAgent(BaseAgent):
    """
    Writes and runs tests to verify the changes made by the Code agent.
    Runs a tool-calling loop, then streams the final test report.
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

    def run(self, prompt: str, plan: str, code_summary: str) -> Iterator[str]:
        """Write tests, run them, and stream the final report."""
        system = _PROMPTS["system"].format(workspace_root=str(self._workspace))
        user_content = (
            f"## Original task\n{prompt}\n\n"
            f"## Implementation plan\n{plan}\n\n"
            f"## Changes made\n{code_summary}\n\n"
            "Run the project's existing tests for the changes above and keep fixing until they pass."
        )
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

        last_content = ""
        for _round in range(self._MAX_TOOL_ROUNDS):
            if self._deadline_reached():
                yield last_content
                return
            try:
                response = self._call_sync(messages, tools=TOOLS_SCHEMA)
            except ContextLengthError:
                shrunk = self._drop_oldest_tool_group(messages)
                if shrunk == messages:
                    yield "Error: context window exceeded while testing."
                    return
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
                if (
                    self._eval_deadline_active()
                    and not self._deadline_reached()
                ):
                    self._push_text_and_nudge(
                        messages, message, last_content, _KEEP_TESTING_NUDGE
                    )
                    continue
                yield from self._yield_final_text(messages, message, last_content)
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
