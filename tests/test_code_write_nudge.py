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


class CodeAgentLoopTests(unittest.TestCase):
    def test_returns_text_when_model_stops_without_tools(self) -> None:
        agent = CodeAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=str(ROOT),
        )
        calls = {"n": 0}

        def fake_sync(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "done", "tool_calls": None},
                    }
                ]
            }

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        self.assertEqual(agent.run("fix it", "edit a.py"), "done")
        self.assertEqual(calls["n"], 1)

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
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "should not run", "tool_calls": None},
                    }
                ]
            }

        agent._call_sync = fake_sync  # type: ignore[method-assign]
        self.assertEqual(agent.run("fix it", "edit a.py"), "")
        self.assertEqual(calls["n"], 0)
