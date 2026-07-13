from __future__ import annotations

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

from core.runners.standard import StandardRunner  # noqa: E402


class _FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str, str]] = []

    def capture_file_update(self, path: str, old_text: str, new_text: str) -> None:
        self.updates.append((path, old_text, new_text))


class RunnerToolWrapperTests(unittest.TestCase):
    def test_standard_write_wrapper_accepts_workspace_root_kwarg(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "example.txt"
            client = _FakeClient()
            runner = StandardRunner("config.yaml", tempdir, client)
            captured: dict[str, object] = {}

            def original_fn(**kwargs):
                captured.update(kwargs)
                path.write_text(str(kwargs["content"]), encoding="utf-8")
                return "ok"

            result = runner._patched_write_file(original_fn)(
                path=str(path),
                content="hello",
                workspace_root=tempdir,
            )

            self.assertEqual(result, "ok")
            self.assertEqual(captured["workspace_root"], tempdir)
            self.assertEqual(client.updates[0], (str(path), "", "hello"))


if __name__ == "__main__":
    unittest.main()
