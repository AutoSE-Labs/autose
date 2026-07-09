"""
Standard TUI adapter.

The Plan -> Code -> Test orchestration lives in core.runners.standard. This
module keeps the terminal-specific state updates, approval handshakes, token
tracking, and diff rendering for the CLI/TUI client.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Union

from core.runners.standard import StandardRunner
from core.tui_bridge import (
    add_artifact,
    complete_session,
    emit_event,
    fail_session,
    note_changed_file,
    note_test,
)
from .display import ChatMessage, Role, TUIState, make_diff_message, patch_execute_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "logic"))


class StandardTUIClient:
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

    def start_stage(self, stage: str, title: str, thinking_label: str) -> None:
        self.state.messages.append(
            ChatMessage(role=Role.STAGE, content="", stage_name=title)
        )
        emit_event(self.state, "stage_started", stage=stage)
        self.state.chat_title = f"AutoSE  —  {title}"
        self.state.thinking = True
        self.state.thinking_label = thinking_label

    def stream_assistant_chunk(self, chunk: str) -> None:
        if self.state.thinking:
            self.state.thinking = False
            self.state.generating = True
            self.state.add_message(Role.ASSISTANT, "")
        self.state.append_to_last(chunk)

    def finish_streaming(self) -> None:
        self.state.generating = False

    def add_assistant_message(self, message: str) -> None:
        self.state.thinking = False
        self.state.generating = False
        self.state.add_message(Role.ASSISTANT, message)

    def request_plan_review(self) -> str:
        self.state.messages.append(ChatMessage(role=Role.PLAN_REVIEW, content=""))
        self.state.chat_title = "AutoSE  —  Plan Review"
        self.state.plan_review = True
        self.state.plan_review_event.clear()
        emit_event(self.state, "approval_requested", kind="plan_review")
        self.state.plan_review_event.wait()

        feedback = self.state.plan_review_response.strip()
        self.state.plan_review = False
        emit_event(
            self.state,
            "approval_resolved",
            kind="plan_review",
            approved=feedback.lower() in ("a", "approve", "y", "yes", ""),
            feedback=feedback,
        )
        return feedback

    def add_user_message(self, message: str) -> None:
        self.state.add_message(Role.USER, message)

    def capture_file_update(self, path: str, old_text: str, new_text: str) -> None:
        self.state.messages.append(make_diff_message(old_text, new_text, path))
        note_changed_file(self.state, path)
        add_artifact(
            self.state,
            "diff",
            f"Updated {Path(path).name}",
            path=path,
            content=new_text,
        )

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

    def note_test(self, name: str, status: str, details: str = "") -> None:
        note_test(self.state, name, status, details)

    def complete(self, summary: str) -> None:
        complete_session(self.state, summary=summary)

    def fail(self, message: str) -> None:
        fail_session(self.state, message)

    def reset_activity(self) -> None:
        self.state.thinking = False
        self.state.generating = False
        self.state.chat_title = "AutoSE  —  Standard"


def run_one(
    state: TUIState,
    prompt: str,
    config_path: Union[str, Path],
    workspace_root: Union[str, Path],
    done_event: threading.Event,
) -> None:
    """Run a full Plan -> Code -> Test pipeline through the core runner."""
    try:
        StandardRunner(
            config_path=config_path,
            workspace_root=workspace_root,
            client=StandardTUIClient(state),
        ).run(prompt)
    finally:
        done_event.set()
