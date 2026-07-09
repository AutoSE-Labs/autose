from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "code"
LOGIC_DIR = CODE_DIR / "logic"

for path in (str(CODE_DIR), str(LOGIC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import main  # noqa: E402


class MainArgTests(unittest.TestCase):
    def test_parse_args_supports_json_and_mode_equals_syntax(self) -> None:
        args = main._parse_args(
            [
                "--json",
                "--events",
                "--mode=lite",
                "--workspace",
                "/tmp/project",
                "hello",
                "world",
            ]
        )
        self.assertTrue(args.json)
        self.assertTrue(args.events)
        self.assertEqual(args.mode, "lite")
        self.assertEqual(args.workspace, Path("/tmp/project"))
        self.assertEqual(args.prompt, ["hello", "world"])

    def test_main_uses_interactive_path_without_headless_flags(self) -> None:
        session_calls: list[tuple[Path, Path, str]] = []

        def fake_session_run(config_path, workspace_root, initial_prompt):
            session_calls.append((config_path, workspace_root, initial_prompt))

        with patch.object(sys, "argv", ["autose", "hello", "world"]):
            with patch("main._import_session", return_value=fake_session_run):
                main.main()

        self.assertEqual(len(session_calls), 1)
        self.assertEqual(session_calls[0][2], "hello world")

    def test_main_routes_json_flag_to_headless_runner(self) -> None:
        run_calls: list[SimpleNamespace] = []
        stdout = io.StringIO()

        def fake_run_headless(args, config_path, prompt):
            run_calls.append(
                SimpleNamespace(args=args, config_path=config_path, prompt=prompt)
            )

        with patch.object(sys, "argv", ["autose", "--json", "--mode=lite", "hello"]):
            with patch("main._run_headless", side_effect=fake_run_headless):
                with patch("sys.stdout", stdout):
                    main.main()

        self.assertEqual(len(run_calls), 1)
        self.assertTrue(run_calls[0].args.headless)
        self.assertTrue(run_calls[0].args.json)
        self.assertEqual(run_calls[0].args.mode, "lite")
        self.assertEqual(run_calls[0].prompt, "hello")

    def test_main_routes_workspace_flag_to_headless_runner(self) -> None:
        run_calls: list[SimpleNamespace] = []

        def fake_run_headless(args, config_path, prompt):
            run_calls.append(
                SimpleNamespace(args=args, config_path=config_path, prompt=prompt)
            )

        with patch.object(
            sys,
            "argv",
            ["autose", "--workspace", "/tmp/project", "inspect"],
        ):
            with patch("main._run_headless", side_effect=fake_run_headless):
                main.main()

        self.assertEqual(len(run_calls), 1)
        self.assertTrue(run_calls[0].args.headless)
        self.assertEqual(run_calls[0].args.workspace, Path("/tmp/project"))
        self.assertEqual(run_calls[0].prompt, "inspect")

    def test_run_headless_events_prints_jsonl_records(self) -> None:
        stdout = io.StringIO()

        def fake_run_headless(
            prompt,
            config_path,
            workspace_root,
            *,
            mode,
            auto_approve_commands,
            stream,
            event_sink,
        ):
            event_sink({"type": "session_started", "data": {"mode": mode}})
            return {"result": {"summary": "done"}}

        args = main._parse_args(["--events", "--mode=lite", "hello"])

        with patch("sys.stdout", stdout):
            with patch("core.clients.run_headless", side_effect=fake_run_headless):
                main._run_headless(args, ROOT / "profiles" / "config.yaml", "hello")

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0]["type"], "event")
        self.assertEqual(lines[0]["event"]["type"], "session_started")
        self.assertEqual(lines[1]["type"], "session")
        self.assertEqual(lines[1]["payload"]["result"]["summary"], "done")


if __name__ == "__main__":
    unittest.main()
