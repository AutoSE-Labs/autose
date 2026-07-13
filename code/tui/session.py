"""
Session loop for AutoSE TUI.

Owns the single Rich Live context for the entire session.  On each prompt:
  1. Builds a plain-text history from TUIState.messages for the classifier.
  2. Classifies the prompt (with history context).
  3. Dispatches to the appropriate runner (lite or standard).
  4. Waits for completion, then loops back for the next prompt.

Complexity only ever increases within a session — once in standard mode a
lite-classified prompt is still handled by the standard runner so the full
pipeline is available.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Union

from rich.live import Live

from core.session import TaskSessionRecorder
from . import lite_tui, standard_tui
from .display import Role, TUIState, build_layout, start_keyboard_listener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "logic"))

from tiers import TIER_RANK


def _history_for_classifier(state: TUIState) -> list[dict]:
    """Return prior session context as plain dicts for the classifier.

    Prefers the MemoryManager's compressed representation (summary + recent
    exchanges) when available, falling back to a simple scan of state.messages.
    """
    memory = state.memory
    if memory is not None and memory.has_context():
        return memory.build_context_messages()

    # Fallback: extract user/assistant messages from the display history.
    result = []
    for msg in state.messages:
        if msg.role == Role.USER:
            result.append({"role": "user", "content": msg.content})
        elif msg.role == Role.ASSISTANT:
            # Truncate long assistant messages to keep the classifier call cheap
            content = msg.content[:500] + "…" if len(msg.content) > 500 else msg.content
            result.append({"role": "assistant", "content": content})
    # Keep at most the last 10 turns (5 exchanges)
    return result[-10:]


def run(
    config_path: Union[str, Path],
    workspace_root: Union[str, Path] = ".",
    initial_prompt: str = "",
) -> None:
    """
    Start the AutoSE TUI session.  Runs until Ctrl-C / Ctrl-D.

    If initial_prompt is given it is submitted immediately; otherwise the
    session starts idle waiting for keyboard input.
    """
    import yaml
    from classifier import TaskClassifier
    from common.memory import MemoryManager

    with open(config_path, "r", encoding="utf-8") as _f:
        _cfg = yaml.safe_load(_f)
    context_limit: int = _cfg.get("inference", {}).get("context_limit", 8192)

    classifier = TaskClassifier(config_path)
    state = TUIState(chat_title="AutoSE")
    state.tokens.context_limit = context_limit
    state.memory = MemoryManager()

    # Track the highest tier reached so far — never downgrade within a session
    current_tier = "lite"

    def _set_title(tier: str) -> None:
        labels = {
            "lite": "AutoSE  —  Lite",
            "standard": "AutoSE  —  Standard",
        }
        state.chat_title = labels.get(tier, f"AutoSE  —  {tier.capitalize()}")

    # Don't set the tier-specific title until the first prompt is processed

    start_keyboard_listener(state)

    with Live(build_layout(state), refresh_per_second=12, screen=True) as live:
        pending_prompt = initial_prompt

        while not state.quit:
            # ----------------------------------------------------------------
            # 1. Read next prompt
            # ----------------------------------------------------------------
            if not pending_prompt:
                state.input_line = None
                state.input_event.clear()
                state.awaiting_input = True

                while not state.input_event.wait(timeout=0.08):
                    live.update(build_layout(state))
                    if state.quit:
                        break
                state.awaiting_input = False
                live.update(build_layout(state))

                prompt = state.input_line
                if state.quit:
                    break
                prompt = prompt.strip() if prompt else ""
                if not prompt:
                    continue
            else:
                prompt = pending_prompt
                pending_prompt = ""

            # ----------------------------------------------------------------
            # 2. Classify with history context
            # ----------------------------------------------------------------
            state.thinking = True
            state.thinking_label = "Classifying"
            live.update(build_layout(state))

            classify_done = threading.Event()
            classified_tier: list[str] = ["lite"]

            def _classify():
                history = _history_for_classifier(state)
                try:
                    classified_tier[0] = classifier.classify(prompt, history=history)
                except Exception:
                    classified_tier[0] = current_tier  # fall back to current on error
                classify_done.set()

            threading.Thread(target=_classify, daemon=True).start()

            try:
                while not classify_done.wait(timeout=0.08):
                    live.update(build_layout(state))
            except KeyboardInterrupt:
                state.interrupted = True

            state.thinking = False

            # If Ctrl-C was pressed during classify, skip running the agent
            if state.interrupted:
                state.interrupted = False
                state.current_input = ""
                continue

            # Ratchet up — never downgrade; set title on first prompt
            new_tier = classified_tier[0]
            if TIER_RANK.get(new_tier, 0) > TIER_RANK.get(current_tier, 0):
                current_tier = new_tier
            _set_title(current_tier)

            state.task_session = TaskSessionRecorder(
                task=prompt,
                workspace_root=str(workspace_root),
                mode=current_tier,
            )
            state.task_session.emit(
                "task_classified",
                prompt=prompt,
                classified_tier=classified_tier[0],
                effective_tier=current_tier,
            )
            state.last_task_result = None

            # ----------------------------------------------------------------
            # 3. Show user message, dispatch to runner
            # ----------------------------------------------------------------
            state.add_message(Role.USER, prompt)
            state.current_input = ""
            live.update(build_layout(state))

            done_event = threading.Event()

            if current_tier == "standard":
                runner = standard_tui.run_one
            else:
                runner = lite_tui.run_one

            t_run = threading.Thread(
                target=runner,
                args=(state, prompt, config_path, workspace_root, done_event),
                daemon=True,
            )
            t_run.start()

            try:
                while not done_event.wait(timeout=0.08):
                    live.update(build_layout(state))

                    # Ctrl-C while agent runs: interrupt and re-prompt
                    if state.interrupted:
                        break
            except KeyboardInterrupt:
                state.interrupted = True

            live.update(build_layout(state))

            if state.interrupted:
                state.interrupted = False
                state.thinking = False
                state.thinking_label = "Thinking"
                state.plan_review = False
                state.cmd_approval = False
                state.current_input = ""
                if state.task_session is not None:
                    state.task_session.fail("Task interrupted before completion.")
                    state.last_task_result = state.task_session.result
                    state.task_session = None
                # Runner thread is still alive as a daemon — let it finish silently
                continue

            t_run.join(timeout=5)
            if state.task_session is not None:
                state.last_task_result = state.task_session.result
                state.task_session = None
