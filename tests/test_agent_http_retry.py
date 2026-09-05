from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "code"
LOGIC_DIR = CODE_DIR / "logic"

for path in (str(CODE_DIR), str(LOGIC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from logic.common.agent import (  # noqa: E402
    BaseAgent,
    InferenceTimeoutError,
    _is_retryable_disconnect,
)


class _OkResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": "ok", "tool_calls": None}}]}
        ).encode()


class AgentHttpRetryTests(unittest.TestCase):
    def test_remote_end_closed_is_retryable(self) -> None:
        exc = urllib.error.URLError("Remote end closed connection without response")
        self.assertTrue(_is_retryable_disconnect(exc))

    def test_call_sync_retries_disconnect_then_succeeds(self) -> None:
        agent = BaseAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
        )
        attempts = {"n": 0}

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.URLError(
                    "Remote end closed connection without response"
                )
            return _OkResponse()

        with (
            patch("logic.common.agent.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("logic.common.agent.time.sleep", return_value=None),
        ):
            result = agent._call_sync([{"role": "user", "content": "hi"}])
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

    def test_call_sync_gives_up_after_four_disconnects(self) -> None:
        agent = BaseAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
        )

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("Remote end closed connection without response")

        with (
            patch("logic.common.agent.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("logic.common.agent.time.sleep", return_value=None),
        ):
            with self.assertRaises(InferenceTimeoutError):
                agent._call_sync([{"role": "user", "content": "hi"}])


class ParseTextToolCallsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = BaseAgent(base_url="http://127.0.0.1:9/v1", api_key="", model="m")
        self.agent._tools = {"read_file": object()}

    def test_none_content_does_not_raise(self) -> None:
        self.assertIsNone(self.agent._parse_text_tool_calls(None))
        self.assertIsNone(self.agent._assistant_tool_calls({"content": None}))

    def test_null_content_with_native_tool_calls(self) -> None:
        calls = [{"id": "1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
        got = self.agent._assistant_tool_calls({"content": None, "tool_calls": calls})
        self.assertEqual(got, calls)

    def test_parses_text_fallback(self) -> None:
        got = self.agent._parse_text_tool_calls("call:read_file{path:/app/README.md}")
        self.assertEqual(got[0]["function"]["name"], "read_file")


if __name__ == "__main__":
    unittest.main()
