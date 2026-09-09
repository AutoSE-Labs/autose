from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "code"
LOGIC_DIR = CODE_DIR / "logic"

for path in (str(CODE_DIR), str(LOGIC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from standard.code.agent import CodeAgent  # noqa: E402
from standard.test.agent import TestAgent  # noqa: E402


def _text_reply(content: str = "done") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": content, "tool_calls": None},
            }
        ]
    }


def _write_reply() -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path": "a.py", "content": "x"}',
                            },
                        }
                    ],
                },
            }
        ]
    }


class CodeAgentLoopTests(unittest.TestCase):
    def test_nudges_then_returns_when_model_stops_without_writes(self) -> None:
        agent = CodeAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=str(ROOT),
        )
        calls = {"n": 0}

        def fake_sync(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            return _text_reply("done")

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        self.assertEqual(agent.run("fix it", "edit a.py"), "done")
        self.assertEqual(calls["n"], 9)

    def test_eval_deadline_nudges_after_a_write(self) -> None:
        os.environ["AUTOSE_DEADLINE_UNIX"] = "9999999999"
        self.addCleanup(os.environ.pop, "AUTOSE_DEADLINE_UNIX", None)
        agent = CodeAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=str(ROOT),
        )
        calls = {"n": 0}

        def fake_sync(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _write_reply()
            return _text_reply("all done")

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        agent._execute_tool = lambda _tc: "ok"  # type: ignore[method-assign]
        self.assertEqual(agent.run("fix it", "edit a.py"), "all done")
        self.assertEqual(calls["n"], 5)

    def test_stops_when_wall_deadline_has_passed(self) -> None:
        os.environ["AUTOSE_DEADLINE_UNIX"] = "0"
        self.addCleanup(os.environ.pop, "AUTOSE_DEADLINE_UNIX", None)
        agent = CodeAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=str(ROOT),
        )
        calls = {"n": 0}

        def fake_sync(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            return _text_reply("should not run")

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        self.assertEqual(agent.run("fix it", "edit a.py"), "")
        self.assertEqual(calls["n"], 0)


class TestAgentLoopTests(unittest.TestCase):
    def test_interactive_text_only_still_stops(self) -> None:
        agent = TestAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=str(ROOT),
        )
        calls = {"n": 0}

        def fake_sync(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            return _text_reply("103 tests pass")

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        self.assertEqual(
            "".join(agent.run("fix it", "plan", "changed a.py")),
            "103 tests pass",
        )
        self.assertEqual(calls["n"], 1)

    def test_eval_deadline_keeps_going_after_text_only(self) -> None:
        os.environ["AUTOSE_DEADLINE_UNIX"] = "9999999999"
        self.addCleanup(os.environ.pop, "AUTOSE_DEADLINE_UNIX", None)
        agent = TestAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=str(ROOT),
        )
        calls = {"n": 0}

        def fake_sync(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            return _text_reply("103 tests pass")

        def reached() -> bool:
            return calls["n"] >= 3

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        agent._deadline_reached = reached  # type: ignore[method-assign]
        out = "".join(agent.run("fix it", "plan", "changed a.py"))
        self.assertEqual(calls["n"], 3)
        self.assertIn("103 tests pass", out)
