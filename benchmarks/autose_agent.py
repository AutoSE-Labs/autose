"""
AutoSE adapter for Harbor / Terminal-Bench 2.1.

External Agent: the agent process runs on the host machine, proxying every
file and command operation into the Docker container via Harbor's
environment.exec() / upload_file() / download_file() APIs.

Pipeline: Plan → Code → Test  (AutoSE Standard)
  Plan   – explores the container environment, produces an implementation plan.
  Code   – executes the plan by writing/editing files in the container.
  Test   – runs checks / tests and streams a final report.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

# ── AutoSE path bootstrap ────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO / "code" / "logic"), str(_REPO / "code")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext
from standard.code.tools import TOOLS_SCHEMA as _CODE_SCHEMA_RAW

# Import only the OpenAI tool schemas (not the Python-side fn impls).
from lite.tools import TOOLS_SCHEMA as _LITE_SCHEMA_RAW
from standard.plan.tools import TOOLS_SCHEMA as _PLAN_SCHEMA_RAW
from standard.test.tools import TOOLS_SCHEMA as _TEST_SCHEMA_RAW

# ── Load AutoSE prompts ───────────────────────────────────────────────────────
_PROMPTS_FILE = _REPO / "code" / "logic" / "prompts.json"
with open(_PROMPTS_FILE, encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)

# ── Constants ─────────────────────────────────────────────────────────────────
_WORKSPACE = "/app"  # default workdir inside TB containers
_MAX_OUTPUT = 4_000  # chars to return from any single tool call
_MAX_ROUNDS = 8  # history pruning window
_CMD_TIMEOUT = 120  # seconds for run_command
_IO_TIMEOUT = 30  # seconds for file / search ops

# ── TB-specific system prompt suffix ─────────────────────────────────────────
# Appended to every stage prompt so the model knows commands run freely.
_TB_SUFFIX = (
    "\n\nIMPORTANT: You are operating inside an isolated Docker container for a "
    "Terminal-Bench evaluation task.  ALL bash commands execute immediately and "
    "directly — there is no user approval step.  Use run_command freely and "
    "aggressively to explore, build, install packages, start services, or verify "
    "your work.  The task files are typically under /app.  Complete the task fully "
    "and leave the container in the required final state."
)


def _tb_prompt(key: str) -> str:
    return _PROMPTS[key]["system"].format(workspace_root=_WORKSPACE) + _TB_SUFFIX


# ── Tool-schema helpers ───────────────────────────────────────────────────────


def _drop_tool(schema: list[dict], name: str) -> list[dict]:
    return [t for t in schema if t["function"]["name"] != name]


# Plan: read-only tools + unrestricted run_command
PLAN_TOOLS = _PLAN_SCHEMA_RAW

# Code: full toolset including write_file + edit_file
CODE_TOOLS = _CODE_SCHEMA_RAW

# Test: read + write + run_command (no edit_file needed)
TEST_TOOLS = _TEST_SCHEMA_RAW

# Lite: read/list/search/find/run_command only
LITE_TOOLS = _LITE_SCHEMA_RAW


# ── LLM call (sync, runs inside a thread executor) ───────────────────────────


def _llm_call(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.2,
) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools

    payload = json.dumps(body).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_txt = ""
        try:
            body_txt = exc.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"LLM HTTP {exc.code}: {body_txt[:300]}") from exc


# ── History pruning ───────────────────────────────────────────────────────────


def _prune(messages: list[dict], max_rounds: int = _MAX_ROUNDS) -> list[dict]:
    if len(messages) <= 2:
        return messages
    head, tail = messages[:2], messages[2:]
    rounds: list[list[dict]] = []
    cur: list[dict] = []
    for msg in tail:
        if msg["role"] == "assistant":
            if cur:
                rounds.append(cur)
            cur = [msg]
        else:
            cur.append(msg)
    if cur:
        rounds.append(cur)
    if len(rounds) <= max_rounds:
        return messages
    dropped = len(rounds) - max_rounds
    kept = [m for r in rounds[-max_rounds:] for m in r]
    notice = {
        "role": "system",
        "content": f"[{dropped} earlier tool-call round(s) were pruned to fit the context window.]",
    }
    return head + [notice] + kept


# ── Container tool dispatcher ────────────────────────────────────────────────


async def _exec_tool(tc: dict, env: BaseEnvironment) -> str:
    """Route a single tool call to the Harbor container via environment APIs."""
    name = tc["function"]["name"]
    try:
        args: dict = json.loads(tc["function"]["arguments"])
    except Exception:
        args = {}

    try:
        # ── run_command ──────────────────────────────────────────────────────
        if name == "run_command":
            cmd = args.get("command", "")
            cwd = args.get("cwd") or None
            result: ExecResult = await env.exec(cmd, cwd=cwd, timeout_sec=_CMD_TIMEOUT)
            out = result.stdout or ""
            if result.stderr:
                out += f"\n[stderr]\n{result.stderr}"
            if result.return_code not in (None, 0):
                out += f"\n[exit {result.return_code}]"
            return out[:_MAX_OUTPUT] or "(no output)"

        # ── read_file ────────────────────────────────────────────────────────
        elif name == "read_file":
            path = args.get("path", "")
            start = args.get("start_line", 1)
            end = args.get("end_line", -1)
            cap = start + 149 if end == -1 else end
            read_cmd = f"awk 'NR>={start} && NR<={cap}' {shlex.quote(path)}"
            total_cmd = f"wc -l < {shlex.quote(path)}"
            read_r, total_r = await asyncio.gather(
                env.exec(read_cmd, timeout_sec=_IO_TIMEOUT),
                env.exec(total_cmd, timeout_sec=10),
            )
            if read_r.return_code != 0:
                return f"Error: {read_r.stderr or f'cannot read {path}'}"
            total = (total_r.stdout or "?").strip()
            header = f"# {path}  (lines {start}–{cap} of {total})\n"
            return header + (read_r.stdout or "")

        # ── list_files ───────────────────────────────────────────────────────
        elif name == "list_files":
            directory = args.get("directory", ".")
            pattern = args.get("pattern", "*")
            recursive = args.get("recursive", False)
            if recursive:
                cmd = (
                    f"find {shlex.quote(directory)} -name {shlex.quote(pattern)}"
                    f" -not -path '*/.*' 2>/dev/null | sort | head -200"
                )
            else:
                cmd = f"ls -1 {shlex.quote(directory)} 2>/dev/null | head -200"
            result = await env.exec(cmd, timeout_sec=_IO_TIMEOUT)
            return result.stdout or f"(empty: {directory})"

        # ── search_files ─────────────────────────────────────────────────────
        elif name == "search_files":
            text = args.get("text", "")
            directory = args.get("directory", ".")
            file_pat = args.get("file_pattern", "*")
            limit = min(args.get("max_results", 50), 100)
            cmd = (
                f"grep -rn --include={shlex.quote(file_pat)} -E"
                f" {shlex.quote(text)} {shlex.quote(directory)} 2>/dev/null"
                f" | head -{limit}"
            )
            result = await env.exec(cmd, timeout_sec=_IO_TIMEOUT)
            return result.stdout or f"No matches for '{text}'"

        # ── find_files ───────────────────────────────────────────────────────
        elif name == "find_files":
            name_pat = args.get("name_pattern", "*")
            directory = args.get("directory", ".")
            cmd = (
                f"find {shlex.quote(directory)} -name {shlex.quote(name_pat)}"
                f" -type f 2>/dev/null | head -200"
            )
            result = await env.exec(cmd, timeout_sec=_IO_TIMEOUT)
            return result.stdout or f"No files matching '{name_pat}'"

        # ── write_file ───────────────────────────────────────────────────────
        elif name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            parent = str(Path(path).parent)
            if parent not in (".", "/"):
                await env.exec(f"mkdir -p {shlex.quote(parent)}", timeout_sec=10)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".tmp", delete=False, encoding="utf-8"
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                await env.upload_file(tmp_path, path)
            finally:
                os.unlink(tmp_path)
            return f"Wrote {len(content.splitlines())} line(s) to {path}"

        # ── edit_file ────────────────────────────────────────────────────────
        elif name == "edit_file":
            path = args.get("path", "")
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            with tempfile.TemporaryDirectory() as tmp_dir:
                local = Path(tmp_dir) / "target"
                await env.download_file(path, local)
                original = local.read_text(encoding="utf-8")
                count = original.count(old_str)
                if count == 0:
                    return f"Error: old_str not found in {path}"
                if count > 1:
                    return f"Error: old_str appears {count} times; be more specific"
                local.write_text(
                    original.replace(old_str, new_str, 1), encoding="utf-8"
                )
                await env.upload_file(local, path)
            return f"Successfully edited {path}"

        else:
            return f"Error: unknown tool '{name}'"

    except Exception as exc:  # noqa: BLE001
        return f"[tool error – {name}]: {exc}"


# ── Async agentic loop ────────────────────────────────────────────────────────


async def _agent_loop(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    tools: list[dict],
    environment: BaseEnvironment,
    log: object,  # callable(str) -> None
    temperature: float = 0.2,
) -> str:
    """Generic async tool-calling loop.  Returns the final answer string."""
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    loop = asyncio.get_event_loop()
    round_n = 0

    while True:
        pruned = _prune(messages)
        try:
            response = await loop.run_in_executor(
                None,
                lambda: _llm_call(base_url, api_key, model, pruned, tools, temperature),
            )
        except Exception as exc:
            msg = f"[LLM error: {exc}]"
            log(msg + "\n")
            return msg

        choice = response["choices"][0]
        message = choice["message"]
        tcs = message.get("tool_calls")

        if not tcs:
            answer = message.get("content", "")
            log(f"[→ final answer, {len(answer)} chars]\n")
            return answer

        round_n += 1
        log(f"  round {round_n}: {len(tcs)} tool call(s)\n")
        messages.append(message)

        for tc in tcs:
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            log(f"    {fn_name}({fn_args[:100]}{'…' if len(fn_args) > 100 else ''})\n")
            result = await _exec_tool(tc, environment)
            log(f"    → {result[:150]}{'…' if len(result) > 150 else ''}\n")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )


# ── Harbor Agent ──────────────────────────────────────────────────────────────


class AutoSEAgent(BaseAgent):
    """
    AutoSE Standard (Plan → Code → Test) wrapped as a Harbor External Agent.

    The agent process runs on the host and proxies every I/O operation into
    the Docker container through Harbor's environment APIs.  No LLM traffic
    touches the container; only bash / file operations do.
    """

    @staticmethod
    def name() -> str:
        return "autose"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass  # Harbor / the Dockerfile already prepares the task environment.

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # ── Load inference config ─────────────────────────────────────────
        cfg_path = _REPO / "profiles" / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        inf = cfg["inference"]
        base_url = inf["base_url"].rstrip("/")
        api_key = inf.get("api_key", "") or ""
        model = inf["model"]

        # ── Per-trial log file ────────────────────────────────────────────
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / "autose.log"

        def log(msg: str) -> None:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(msg)

        short_instr = instruction[:120].replace("\n", " ")
        log(f"{'=' * 72}\nAutoSE Standard  |  {short_instr}…\n{'=' * 72}\n\n")

        shared = dict(
            base_url=base_url,
            api_key=api_key,
            model=model,
            environment=environment,
            log=log,
        )

        # ── Stage 1 – Plan ────────────────────────────────────────────────
        log("━━━ PLAN ━━━\n")
        plan = await _agent_loop(
            **shared,
            system_prompt=_tb_prompt("standard_plan_agent"),
            user_message=instruction,
            tools=PLAN_TOOLS,
        )
        log(f"\n[Plan done — {len(plan)} chars]\n\n")

        # ── Stage 2 – Code ────────────────────────────────────────────────
        log("━━━ CODE ━━━\n")
        code_summary = await _agent_loop(
            **shared,
            system_prompt=_tb_prompt("standard_code_agent"),
            user_message=(
                f"## Original task\n{instruction}\n\n"
                f"## Implementation plan\n{plan}\n\n"
                "Execute the plan.  Read any files you need first, then apply all changes."
            ),
            tools=CODE_TOOLS,
        )
        log(f"\n[Code done — {len(code_summary)} chars]\n\n")

        # ── Stage 3 – Test ────────────────────────────────────────────────
        log("━━━ TEST ━━━\n")
        test_report = await _agent_loop(
            **shared,
            system_prompt=_tb_prompt("standard_test_agent"),
            user_message=(
                f"## Original task\n{instruction}\n\n"
                f"## Changes applied\n{code_summary}\n\n"
                "Verify the implementation is correct.  Run any relevant commands or tests "
                "to confirm the task is fully complete, then write a final report."
            ),
            tools=TEST_TOOLS,
        )
        log(f"\n[Test done — {len(test_report)} chars]\n\n")
        log("━━━ DONE ━━━\n")

        # ── Populate Harbor context ────────────────────────────────────────
        context.metadata = {
            "plan_chars": len(plan),
            "code_summary": code_summary[:600],
            "test_report": test_report[:600],
        }

class LiteAutoSEAgent(BaseAgent):
    """
    AutoSE Lite mode wrapped as a Harbor External Agent.

    This path uses the single-step lite agent behavior: explore the workspace
    with read-only tools plus run_command, then return a concise answer.
    """

    @staticmethod
    def name() -> str:
        return "autose-lite"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        cfg_path = _REPO / "profiles" / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        inf = cfg["inference"]
        base_url = inf["base_url"].rstrip("/")
        api_key = inf.get("api_key", "") or ""
        model = inf["model"]

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / "autose-lite.log"

        def log(msg: str) -> None:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(msg)

        short_instr = instruction[:120].replace("\\n", " ")
        log(f"{'=' * 72}\\nAutoSE Lite  |  {short_instr}…\\n{'=' * 72}\\n\\n")

        answer = await _agent_loop(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=_tb_prompt("lite_agent"),
            user_message=instruction,
            tools=LITE_TOOLS,
            environment=environment,
            log=log,
        )
        log(f"\\n[Lite done — {len(answer)} chars]\\n")
        context.metadata = {"answer_chars": len(answer)}
