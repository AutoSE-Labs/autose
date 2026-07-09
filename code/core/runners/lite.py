from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, Union

import yaml

_CODE_DIR = Path(__file__).resolve().parents[2]
_LOGIC_DIR = _CODE_DIR / "logic"
if str(_LOGIC_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGIC_DIR))


class LiteRunClient(Protocol):
    """Client hooks used by the core lite workflow."""

    def prepare_agent(self, agent: object) -> None:
        """Apply client-specific instrumentation to an agent."""

    def build_session_context(self, call_fn) -> list[dict] | None:
        """Return prior session context for agent prompts."""

    def record_memory_exchange(self, prompt: str, assistant: str) -> None:
        """Persist the completed exchange in client/session memory."""

    def start_thinking(self, label: str) -> None:
        """Notify the client that the agent is working."""

    def stream_assistant_chunk(self, chunk: str) -> None:
        """Render or capture an assistant streaming chunk."""

    def add_assistant_message(self, message: str) -> None:
        """Append a complete assistant message."""

    def add_artifact(
        self,
        kind: str,
        title: str,
        *,
        path: str = "",
        content: str = "",
        **metadata,
    ) -> None:
        """Record a structured artifact."""

    def complete(self, summary: str) -> None:
        """Mark the task session complete."""

    def fail(self, message: str) -> None:
        """Mark the task session failed."""

    def reset_activity(self) -> None:
        """Clear client activity indicators after completion or failure."""


class LiteRunner:
    """Core single-prompt lite runner, independent of terminal rendering."""

    def __init__(
        self,
        config_path: Union[str, Path],
        workspace_root: Union[str, Path],
        client: LiteRunClient,
    ) -> None:
        self.config_path = Path(config_path)
        self.workspace_root = Path(workspace_root)
        self.client = client

    def run(self, prompt: str) -> None:
        from lite.agent import LiteAgent

        with open(self.config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        inference = config.get("inference", {})
        agent = LiteAgent(
            base_url=inference.get("base_url", "http://127.0.0.1:11434/v1"),
            api_key=inference.get("api_key", "") or "",
            model=inference.get("model", ""),
            workspace_root=str(self.workspace_root),
        )

        self.client.prepare_agent(agent)
        session_context = self.client.build_session_context(agent._call_sync)
        self.client.start_thinking("Thinking")

        response_chunks: list[str] = []
        try:
            for chunk in agent.run(prompt, session_context=session_context):
                self.client.stream_assistant_chunk(chunk)
                response_chunks.append(chunk)
        except Exception as exc:
            self.client.fail(f"Error: {exc}")
            self.client.add_assistant_message(f"Error: {exc}")
        else:
            response = "".join(response_chunks)
            if response:
                self.client.record_memory_exchange(prompt, response)
                self.client.add_artifact(
                    "response",
                    "Lite response",
                    content=response,
                )
                self.client.complete(response)
        finally:
            self.client.reset_activity()
