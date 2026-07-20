from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "code"
LOGIC_DIR = CODE_DIR / "logic"

for path in (str(CODE_DIR), str(LOGIC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.clients.headless import HeadlessClient, run_headless  # noqa: E402
from core.session import TaskSessionRecorder  # noqa: E402


class _FakeAgent:
    def __init__(self) -> None:
        self._tools = {"run_command": object()}
        self.calls: list[tuple[str, dict | None]] = []

    def _call_sync(self, messages, tools=None):
        self.calls.append(("sync", {"messages": messages, "tools": tools}))
        return {"usage": {"prompt_tokens": 3, "completion_tokens": 5}}

    def _execute_tool(self, tool_call: dict) -> str:
        self.calls.append(("tool", tool_call))
        return "tool-result"


class _FakeRunner:
    last_init: dict | None = None
    last_prompt: str | None = None

    def __init__(self, config_path, workspace_root, client) -> None:
        type(self).last_init = {
            "config_path": str(config_path),
            "workspace_root": str(workspace_root),
            "client": client,
        }
        self.client = client

    def run(self, prompt: str) -> None:
        type(self).last_prompt = prompt
        self.client.start_thinking("Thinking")
        self.client.stream_assistant_chunk("hello")
        self.client.add_artifact("response", "Lite response", content="hello")
        self.client.complete("hello")
        self.client.reset_activity()


class HeadlessClientTests(unittest.TestCase):
    def test_prepare_agent_tracks_usage_and_denies_commands_by_default(self) -> None:
        recorder = TaskSessionRecorder("task", "/workspace", "lite")
        client = HeadlessClient(recorder)
        agent = _FakeAgent()

        client.prepare_agent(agent)
        result = agent._call_sync([{"role": "user", "content": "hi"}])
        denied = agent._execute_tool(
            {
                "function": {
                    "name": "run_command",
                    "arguments": json.dumps({"command": "echo hi"}),
                }
            }
        )

        self.assertEqual(result["usage"]["prompt_tokens"], 3)
        self.assertEqual(client.prompt_tokens, 3)
        self.assertEqual(client.completion_tokens, 5)
        self.assertIn("denied", denied.lower())

        event_types = [event.type for event in recorder.events]
        self.assertIn("tokens_updated", event_types)
        self.assertIn("energy_updated", event_types)
        self.assertIn("approval_requested", event_types)
        self.assertIn("approval_resolved", event_types)
        self.assertNotIn(("tool",), {(kind,) for kind, _ in agent.calls})
        self.assertGreater(client.energy_joules, 0)
        self.assertIn("energy_joules", client.to_dict()["usage"])

    def test_prepare_agent_allows_commands_when_yes_policy_enabled(self) -> None:
        recorder = TaskSessionRecorder("task", "/workspace", "lite")
        client = HeadlessClient(recorder, auto_approve_commands=True)
        agent = _FakeAgent()

        client.prepare_agent(agent)
        allowed = agent._execute_tool(
            {
                "function": {
                    "name": "run_command",
                    "arguments": json.dumps({"command": "echo hi"}),
                }
            }
        )

        self.assertEqual(allowed, "tool-result")
        event_types = [event.type for event in recorder.events]
        self.assertIn("tool_called", event_types)

    def test_run_headless_uses_selected_runner_and_returns_session_payload(self) -> None:
        with patch("core.clients.headless.LiteRunner", _FakeRunner):
            result = run_headless(
                "hello world",
                config_path=ROOT / "profiles" / "config.yaml",
                workspace_root=ROOT,
                mode="lite",
            )

        self.assertEqual(_FakeRunner.last_prompt, "hello world")
        self.assertEqual(result["task"], "hello world")
        self.assertEqual(result["mode"], "lite")
        self.assertEqual(result["result"]["status"], "completed")
        self.assertEqual(result["result"]["summary"], "hello")
        self.assertEqual(result["artifacts"][0]["kind"], "response")

    def test_run_headless_streams_structured_events_to_sink(self) -> None:
        events: list[dict] = []

        with patch("core.clients.headless.LiteRunner", _FakeRunner):
            result = run_headless(
                "hello world",
                config_path=ROOT / "profiles" / "config.yaml",
                workspace_root=ROOT,
                mode="lite",
                event_sink=events.append,
            )

        event_types = [event["type"] for event in events]
        self.assertEqual(event_types[0], "session_started")
        self.assertIn("agent_stage_started", event_types)
        self.assertIn("assistant_chunk", event_types)
        self.assertIn("artifact_created", event_types)
        self.assertIn("session_completed", event_types)
        self.assertEqual(result["result"]["summary"], "hello")


if __name__ == "__main__":
    unittest.main()
