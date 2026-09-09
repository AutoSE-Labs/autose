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

    def test_parses_json_tool_call_xml(self) -> None:
        blob = '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'
        got = self.agent._parse_text_tool_calls(blob)
        self.assertEqual(got[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(got[0]["function"]["arguments"])["path"], "a.py")

    def test_parses_reasoning_content(self) -> None:
        got = self.agent._assistant_tool_calls(
            {
                "content": None,
                "reasoning_content": '<tool_call>{"name": "read_file", "arguments": {"path": "b.py"}}</tool_call>',
            }
        )
        self.assertEqual(got[0]["function"]["name"], "read_file")

    def test_execute_tool_accepts_dict_arguments(self) -> None:
        self.agent._tools = {
            "read_file": lambda workspace_root, **kwargs: json.dumps(kwargs)
        }
        result = self.agent._execute_tool(
            {"function": {"name": "read_file", "arguments": {"path": "x.py"}}}
        )
        self.assertIn("x.py", result)

    def test_call_sync_sets_tool_choice_and_extras(self) -> None:
        agent = BaseAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            request_extras={"chat_template_kwargs": {"enable_thinking": False}},
        )
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            captured["ua"] = req.get_header("User-agent")
            captured["session"] = req.get_header("X-opencode-session")
            return _OkResponse()

        with patch("logic.common.agent.urllib.request.urlopen", side_effect=fake_urlopen):
            agent._call_sync(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "read_file"}}],
            )
        self.assertEqual(captured["body"]["tool_choice"], "auto")
        self.assertEqual(
            captured["body"]["chat_template_kwargs"], {"enable_thinking": False}
        )
        self.assertEqual(captured["ua"], "autose/1.0")
        self.assertTrue(captured["session"])

    def test_http_error_includes_response_body(self) -> None:
        agent = BaseAgent(base_url="http://127.0.0.1:9/v1", api_key="", model="m")

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:9/v1/chat/completions",
                400,
                "Bad Request",
                hdrs=None,
                fp=__import__("io").BytesIO(b'{"error":{"type":"MissingSessionID"}}'),
            )

        with patch("logic.common.agent.urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                agent._call_sync([{"role": "user", "content": "hi"}])
        self.assertIn("MissingSessionID", str(ctx.exception))

    def test_apply_output_limit_when_reserved_is_zero(self) -> None:
        agent = BaseAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            context_limit=262144,
            reserved_output_tokens=0,
        )
        body = {"messages": [{"role": "user", "content": "hi"}]}
        agent._apply_output_limit(body)
        self.assertIn("max_tokens", body)
        self.assertGreaterEqual(body["max_tokens"], 256)
        self.assertLess(body["max_tokens"], 262144)

    def test_call_sync_retries_empty_400_after_dropping_history(self) -> None:
        agent = BaseAgent(base_url="http://127.0.0.1:9/v1", api_key="", model="m")
        attempts = {"n": 0}
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "old"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "new"},
        ]

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError(
                    "http://127.0.0.1:9/v1/chat/completions",
                    400,
                    "Bad Request",
                    hdrs=None,
                    fp=__import__("io").BytesIO(b""),
                )
            return _OkResponse()

        with patch("logic.common.agent.urllib.request.urlopen", side_effect=fake_urlopen):
            result = agent._call_sync(messages)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

    def test_execute_tool_skips_third_identical_call(self) -> None:
        agent = BaseAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            workspace_root=".",
        )
        agent._tools = {"read_file": lambda workspace_root, **kwargs: "ok"}
        call = {
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "/app/x.py", "start_line": 1}),
            }
        }
        self.assertEqual(agent._execute_tool(call), "ok")
        self.assertEqual(agent._execute_tool(call), "ok")
        skipped = agent._execute_tool(call)
        self.assertTrue(skipped.startswith("Error: identical tool call"))

    def test_compaction_merges_notice_into_leading_system(self) -> None:
        agent = BaseAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="",
            model="m",
            context_limit=400,
            reserved_output_tokens=0,
        )
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
        ]
        for index in range(12):
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": str(index)}],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(index),
                    "content": "x" * 400,
                }
            )
        prepared = agent._prepare_messages(messages)
        system_roles = [msg.get("role") for msg in prepared]
        self.assertEqual(system_roles[0], "system")
        self.assertNotIn("system", system_roles[1:])
        self.assertIn("omitted to fit", prepared[0]["content"])

    def test_prune_merges_notice_into_leading_system(self) -> None:
        agent = BaseAgent(base_url="http://127.0.0.1:9/v1", api_key="", model="m")
        agent._MAX_HISTORY_ROUNDS = 2
        messages = [{"role": "system", "content": "sys"}]
        for index in range(5):
            messages.append({"role": "user", "content": f"u{index}"})
            messages.append({"role": "assistant", "content": f"a{index}"})
        pruned = agent._prune_messages(messages)
        roles = [msg["role"] for msg in pruned]
        self.assertEqual(roles[0], "system")
        self.assertNotIn("system", roles[1:])
        self.assertIn("dropped", pruned[0]["content"])
        self.assertEqual(messages[0]["content"], "sys")

    def test_coerce_folds_second_system_into_first(self) -> None:
        folded = BaseAgent._coerce_single_leading_system(
            [
                {"role": "system", "content": "sys"},
                {"role": "system", "content": "notice"},
                {"role": "user", "content": "task"},
            ]
        )
        self.assertEqual([msg["role"] for msg in folded], ["system", "user"])
        self.assertIn("notice", folded[0]["content"])

    def test_session_id_from_env(self) -> None:
        with patch.dict("os.environ", {"AUTOSE_SESSION_ID": "sess-from-env"}):
            agent = BaseAgent(base_url="http://127.0.0.1:9/v1", api_key="", model="m")
        self.assertEqual(agent._headers()["x-opencode-session"], "sess-from-env")


if __name__ == "__main__":
    unittest.main()
