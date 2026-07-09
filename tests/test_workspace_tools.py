from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "code"
LOGIC_DIR = CODE_DIR / "logic"

for path in (str(CODE_DIR), str(LOGIC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from common.agent import BaseAgent  # noqa: E402
from common.tool import Tool  # noqa: E402
from common.tools import _read_file, _run_command  # noqa: E402


class _WorkspaceAgent(BaseAgent):
    def __init__(self, workspace_root: str) -> None:
        super().__init__(
            base_url="http://example.invalid/v1",
            api_key="",
            model="test-model",
            workspace_root=workspace_root,
        )
        self._tools = {
            "read_file": Tool(
                name="read_file",
                description="Read a file.",
                parameters={"path": {"type": "string"}},
                required=["path"],
                fn=_read_file,
            ),
            "run_command": Tool(
                name="run_command",
                description="Run a command.",
                parameters={"command": {"type": "string"}},
                required=["command"],
                fn=_run_command,
            ),
        }


class WorkspaceToolTests(unittest.TestCase):
    def test_execute_tool_reads_relative_to_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)
            (workspace_path / "src").mkdir()
            (workspace_path / "src" / "hello.txt").write_text("hello\n", encoding="utf-8")

            agent = _WorkspaceAgent(workspace)
            output = agent._execute_tool(
                {
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "src/hello.txt"}),
                    }
                }
            )

            self.assertIn("# src/hello.txt", output)
            self.assertIn("hello", output)

    def test_read_file_rejects_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            outside = Path(workspace).parent / "outside.txt"
            outside.write_text("nope\n", encoding="utf-8")

            output = _read_file(str(outside), workspace_root=workspace)

            self.assertIn("escapes workspace root", output)
            outside.unlink(missing_ok=True)

    def test_run_command_rejects_parent_path_and_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            parent_output = _run_command(
                "find ../ -name '*.py'",
                workspace_root=workspace,
            )
            absolute_output = _run_command(
                "find / -name '*.py'",
                workspace_root=workspace,
            )

            self.assertIn("parent path", parent_output)
            self.assertIn("absolute path", absolute_output)

    def test_run_command_rejects_cwd_escape(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            output = _run_command("pwd", cwd="..", workspace_root=workspace)
            self.assertIn("escapes workspace root", output)

    def test_run_command_allows_relative_workspace_commands(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)
            (workspace_path / "nested").mkdir()
            (workspace_path / "nested" / "file.txt").write_text("ok\n", encoding="utf-8")

            output = _run_command(
                "find . -name 'file.txt'",
                cwd="nested",
                workspace_root=workspace,
            )

            self.assertIn("./file.txt", output)
            self.assertIn("Exit code: 0", output)


if __name__ == "__main__":
    unittest.main()
