from __future__ import annotations

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
from standard.plan.agent import PlanAgent  # noqa: E402
from standard.test.agent import TestAgent as StandardTestAgent  # noqa: E402


class StandardContextConfigTests(unittest.TestCase):
    def test_all_standard_agents_accept_context_budget(self) -> None:
        for agent_cls in (PlanAgent, CodeAgent, StandardTestAgent):
            with self.subTest(agent=agent_cls.__name__):
                agent = agent_cls(
                    base_url="http://127.0.0.1:8000/v1",
                    api_key="",
                    model="test-model",
                    workspace_root=str(ROOT),
                    context_limit=65536,
                    reserved_output_tokens=4096,
                )
                self.assertEqual(agent._context_limit, 65536)
                self.assertEqual(agent._reserved_output_tokens, 4096)


if __name__ == "__main__":
    unittest.main()
