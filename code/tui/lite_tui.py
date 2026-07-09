"""
Lite TUI adapter.

The single-prompt lite workflow lives in core.runners.lite. This module keeps
terminal-specific state updates, token tracking, tool logging, and result
recording for the CLI/TUI client.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Union

from core.runners.lite import LiteRunner
from core.tui_bridge import add_artifact, complete_session, emit_event, fail_session
from .display import Role, TUIState, patch_execute_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "logic"))


class LiteTUIClient:
    def __init__(self, state: TUIState) -> None:
        self.state = state

    def prepare_agent(self, agent: object) -> None:
        original = agent._call_sync

        def tracked(messages, tools=None):
            result = original(messages, tools=tools)
            usage = result.get("usage")
            if usage:
                self.state.tokens.update(usage)
            return result

        agent._call_sync = tracked  # type: ignore[method-assign]
        patch_execute_tool(agent, self.state)

    def build_session_context(self, call_fn) -> list[dict] | None:
        memory = self.state.memory
        if memory is None:
            return None

        memory.set_call_fn(call_fn)
        return memory.build_context_messages() if memory.has_context() else None

    def record_memory_exchange(self, prompt: str, assistant: str) -> None:
        memory = self.state.memory
        if memory is not None:
            memory.add_exchange(prompt, assistant)

    def start_thinking(self, label: str) -> None:
        self.state.thinking = True
        self.state.thinking_label = label
        emit_event(self.state, "agent_stage_started", stage="lite", label=label)

    def stream_assistant_chunk(self, chunk: str) -> None:
        if self.state.thinking:
            self.state.thinking = False
            self.state.generating = True
        self.state.append_to_last(chunk)

    def add_assistant_message(self, message: str) -> None:
        self.state.thinking = False
        self.state.generating = False
        self.state.add_message(Role.ASSISTANT, message)

    def add_artifact(
        self,
        kind: str,
        title: str,
        *,
        path: str = "",
        content: str = "",
        **metadata,
    ) -> None:
        add_artifact(
            self.state,
            kind,
            title,
            path=path,
            content=content,
            **metadata,
        )

    def complete(self, summary: str) -> None:
        complete_session(self.state, summary=summary)

    def fail(self, message: str) -> None:
        fail_session(self.state, message)

    def reset_activity(self) -> None:
        self.state.thinking = False
        self.state.generating = False


def run_one(
    state: TUIState,
    prompt: str,
    config_path: Union[str, Path],
    workspace_root: Union[str, Path],
    done_event: threading.Event,
) -> None:
    """Run one LiteAgent call through the core runner."""
    try:
        LiteRunner(
            config_path=config_path,
            workspace_root=workspace_root,
            client=LiteTUIClient(state),
        ).run(prompt)
    finally:
        done_event.set()
