"""Pier/Harbor installed-agent adapter that runs AutoSE headless on DeepSWE.

Load it with:

    PYTHONPATH=eval/deepswe pier run ... --agent-import-path autose_agent:AutoSEAgent

The agent is installed into the task image at build time (network is
available then), and at run time it reaches the inference server only through
Pier's egress proxy, whose allowlist is derived from ``base_url``.
"""

from __future__ import annotations

import json
import shlex
import uuid
from pathlib import Path
from typing import Any

import yaml

from pier.agents.installed.base import BaseInstalledAgent, with_prompt_template
from pier.agents.network import allowlist_from_urls
from pier.environments import agent_setup
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist

# Pier's egress proxy is squid with its default 15-minute read_timeout. AutoSE
# makes non-streaming calls, and a long thinking response from a self-hosted
# model shared by several trials can take longer than that, which surfaces as
# HTTP 504 and ends the run. Raise it well above any single model call.
_SQUID_READ_TIMEOUT = "read_timeout 60 minutes\nrequest_timeout 60 minutes\n"
_original_squid_bootstrap = agent_setup.squid_bootstrap_command


def _squid_bootstrap_with_timeouts() -> str:
    script = _original_squid_bootstrap()
    anchor = "cache deny all\n"
    if anchor not in script:
        raise RuntimeError("Pier's squid config changed; update the timeout patch.")
    return script.replace(anchor, anchor + _SQUID_READ_TIMEOUT, 1)


agent_setup.squid_bootstrap_command = _squid_bootstrap_with_timeouts

_HOME = "/opt/autose"
_EVENTS_FILE = "autose.jsonl"
_STDERR_FILE = "autose.stderr"
_USAGE_FILE = "usage.json"
_WORKSPACE = "/app"


class AutoSEAgent(BaseInstalledAgent):
    """Runs ``autose --events --yes`` against the task checkout in /app."""

    def __init__(
        self,
        *args,
        base_url: str | None = None,
        api_key: str | None = None,
        repo_url: str = "https://github.com/AutoSE-Labs/autose",
        ref: str = "main",
        mode: str = "standard",
        context_limit: int = 131072,
        reserved_output_tokens: int = 32768,
        wall_timeout_sec: int = 5400,
        request_extras: dict[str, Any] | str | None = None,
        commit_fallback: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._base_url = base_url or self._get_env("OPENAI_BASE_URL") or ""
        self._api_key = api_key or self._get_env("OPENAI_API_KEY") or ""
        self._repo_url = repo_url
        self._ref = ref
        self._mode = mode
        self._context_limit = int(context_limit)
        self._reserved_output_tokens = int(reserved_output_tokens)
        self._wall_timeout_sec = int(wall_timeout_sec)
        if isinstance(request_extras, str):
            request_extras = json.loads(request_extras)
        self._request_extras = dict(request_extras or {})
        self._commit_fallback = bool(commit_fallback)

    @staticmethod
    def name() -> str:
        return "autose"

    def _served_model(self) -> str:
        model = self.model_name or ""
        # Pier model names are provider/model; the server only knows the model.
        if model.startswith("openai/"):
            model = model[len("openai/") :]
        return model

    def get_version_command(self) -> str | None:
        return f"git -C {_HOME}/repo rev-parse --short HEAD"

    def install_spec(self) -> AgentInstallSpec:
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self._ref,
            steps=[
                InstallStep(
                    user="root",
                    env={"DEBIAN_FRONTEND": "noninteractive"},
                    run=(
                        "(command -v git && command -v curl) >/dev/null || "
                        "(apt-get update && apt-get install -y --no-install-recommends "
                        "git curl ca-certificates)"
                    ),
                ),
                InstallStep(
                    user="root",
                    env={"UV_INSTALL_DIR": f"{_HOME}/bin", "UV_PYTHON_INSTALL_DIR": f"{_HOME}/python"},
                    run=(
                        "set -euo pipefail; "
                        f"mkdir -p {_HOME} && "
                        "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                        f"git init -q {_HOME}/repo && cd {_HOME}/repo && "
                        f"git remote add origin {shlex.quote(self._repo_url)} && "
                        f"git fetch -q --depth 1 origin {shlex.quote(self._ref)} && "
                        "git checkout -q FETCH_HEAD && "
                        f"{_HOME}/bin/uv sync --frozen --python 3.13 --no-dev && "
                        f"chmod -R a+rX {_HOME} && "
                        f"{_HOME}/repo/.venv/bin/python -c 'import yaml, rich'"
                    ),
                ),
            ],
            verification_command=f"test -x {_HOME}/repo/.venv/bin/autose",
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return allowlist_from_urls([self._base_url])

    def _config_yaml(self) -> str:
        inference: dict[str, Any] = {
            "provider": "openai",
            "base_url": self._base_url,
            "api_key": self._api_key,
            "model": self._served_model(),
            "context_limit": self._context_limit,
            "reserved_output_tokens": self._reserved_output_tokens,
        }
        if self._request_extras:
            inference["request_extras"] = self._request_extras
        return yaml.safe_dump(
            {"inference": inference, "workspace": {"root": _WORKSPACE}},
            sort_keys=False,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self._base_url:
            raise ValueError("AutoSE needs base_url (agent kwarg or OPENAI_BASE_URL).")

        config_path = "/tmp/autose-config.yaml"
        await self.exec_as_agent(
            environment,
            command=f"printf '%s' {shlex.quote(self._config_yaml())} > {config_path}",
        )

        env = self.build_process_env(
            {
                "AUTOSE_CONFIG": config_path,
                "AUTOSE_WALL_TIMEOUT_SEC": str(self._wall_timeout_sec),
                "AUTOSE_USAGE_PATH": f"/logs/agent/{_USAGE_FILE}",
                "AUTOSE_SESSION_ID": str(uuid.uuid4()),
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = (
            f"cd {_WORKSPACE} && {_HOME}/repo/.venv/bin/autose --events --yes "
            f"--mode {shlex.quote(self._mode)} --workspace {_WORKSPACE} "
            f"-- {shlex.quote(instruction)} "
            f"2>/logs/agent/{_STDERR_FILE} </dev/null | tee /logs/agent/{_EVENTS_FILE}"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=env)
        finally:
            if self._commit_fallback:
                # Grading diffs base_commit..HEAD, so uncommitted edits would be
                # silently dropped. The task prompt asks the agent to commit;
                # this only catches work it left in the tree. AutoSE's own
                # .autose/ scratch (plans, artifacts) is not part of the answer.
                await environment.exec(
                    command=(
                        f"cd {_WORKSPACE} && git config --global --add safe.directory {_WORKSPACE}; "
                        "git add -A -- . ':(exclude).autose' && git -c user.name=autose -c user.email=autose@localhost "
                        "commit -q -m 'AutoSE: commit remaining changes' || true"
                    )
                )

    def _session_payload(self) -> dict[str, Any] | None:
        path = self.logs_dir / _EVENTS_FILE
        if not path.exists():
            return None
        payload = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "session":
                payload = record.get("payload")
        return payload

    def _count_tool_calls(self) -> int:
        path = self.logs_dir / _EVENTS_FILE
        if not path.exists():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"tool_called"' in line:
                count += 1
        return count

    def populate_context_post_run(self, context: AgentContext) -> None:
        payload = self._session_payload()
        usage: dict[str, Any] = (payload or {}).get("usage") or {}
        usage_path = Path(self.logs_dir) / _USAGE_FILE
        if not usage and usage_path.exists():
            # The process was cut off before the final session line; fall back
            # to the running totals AutoSE writes after every model call.
            try:
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                usage = {}
        if usage:
            context.n_input_tokens = int(usage.get("prompt_tokens") or 0)
            context.n_output_tokens = int(usage.get("completion_tokens") or 0)
            context.n_cache_tokens = int(usage.get("cached_tokens") or 0)
            context.cost_usd = 0.0
        context.n_agent_steps = self._count_tool_calls()


# Logs every model completion of the Pro edition, which reports usage only in
# its final result line and so records nothing when a run is stopped early.
_PRO_SITECUSTOMIZE = '''
import json, os, time
_path = os.environ.get("AUTOSE_TRACE_PATH")
if _path:
    try:
        from logic.inference import openai_compatible as _oc
        _orig = _oc.OpenAICompatibleChatModel.complete
        def _complete(self, *args, **kwargs):
            started = time.time()
            result = _orig(self, *args, **kwargs)
            try:
                reasoning = result.reasoning or ""
                with open(_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "t_start": started, "t_end": time.time(),
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                        "finish_reason": result.finish_reason,
                        "tool_calls": [c.name for c in result.tool_calls],
                        "content_chars": len(result.content or ""),
                        "reasoning_chars": len(reasoning),
                        "reasoning_tail": reasoning[-600:],
                        "max_output_tokens": kwargs.get("max_output_tokens"),
                    }) + "\\n")
            except Exception:
                pass
            return result
        _oc.OpenAICompatibleChatModel.complete = _complete
    except Exception:
        pass
'''


class AutoSEProAgent(AutoSEAgent):
    """Runs the Pro edition (``autose --json --yes --tier``) from a source tarball.

    The Pro repository is private, so the task image cannot clone it. Instead
    ``source_url`` points at a tarball served to the Docker build network.
    Pro checks its own time budget only between stages, so the wall clock is
    enforced from outside with SIGINT, which Pro treats as "stop and keep the
    work done so far".
    """

    _TRACE_FILE = "trace.jsonl"
    _OUTPUT_FILE = "autose-pro.jsonl"

    def __init__(self, *args, source_url: str = "", tier: str = "craft", **kwargs):
        super().__init__(*args, **kwargs)
        if not source_url:
            raise ValueError("AutoSEProAgent needs source_url (a .tar.gz of the repo).")
        self._source_url = source_url
        self._tier = tier

    @staticmethod
    def name() -> str:
        return "autose-pro"

    def get_version_command(self) -> str | None:
        return f"cat {_HOME}/pro/VERSION"

    def install_spec(self) -> AgentInstallSpec:
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self._ref,
            steps=[
                InstallStep(
                    user="root",
                    env={"DEBIAN_FRONTEND": "noninteractive"},
                    run=(
                        "command -v curl >/dev/null || "
                        "(apt-get update && apt-get install -y --no-install-recommends curl ca-certificates)"
                    ),
                ),
                InstallStep(
                    user="root",
                    env={"UV_INSTALL_DIR": f"{_HOME}/bin", "UV_PYTHON_INSTALL_DIR": f"{_HOME}/python"},
                    run=(
                        "set -euo pipefail; "
                        f"mkdir -p {_HOME}/pro/src && "
                        "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                        f"curl -fsSL {shlex.quote(self._source_url)} | tar -xz -C {_HOME}/pro/src && "
                        f"echo {shlex.quote(self._ref)} > {_HOME}/pro/VERSION && "
                        f"{_HOME}/bin/uv venv -q --python 3.13 {_HOME}/pro/venv && "
                        f"VIRTUAL_ENV={_HOME}/pro/venv {_HOME}/bin/uv pip install -q {_HOME}/pro/src && "
                        f"printf '%s' {shlex.quote(_PRO_SITECUSTOMIZE)} > "
                        f"$({_HOME}/pro/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()[\"purelib\"])')/sitecustomize.py && "
                        f"chmod -R a+rX {_HOME}"
                    ),
                ),
            ],
            verification_command=f"test -x {_HOME}/pro/venv/bin/autose",
        )

    def _config_yaml(self) -> str:
        temperature = self._request_extras.get("temperature", 1.0)
        return yaml.safe_dump(
            {
                "inference": {
                    "provider": "openai-compatible",
                    "base_url": self._base_url,
                    "api_key": self._api_key,
                    "model": self._served_model(),
                    "context_limit": self._context_limit,
                    "temperature": temperature,
                    # Default is 900 s, which cuts off long responses on a shared GPU.
                    "timeout": 3600,
                },
            },
            sort_keys=False,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self._base_url:
            raise ValueError("AutoSE needs base_url (agent kwarg or OPENAI_BASE_URL).")

        config_path = "/tmp/autose-pro.yaml"
        await self.exec_as_agent(
            environment,
            command=f"printf '%s' {shlex.quote(self._config_yaml())} > {config_path}",
        )
        env = self.build_process_env(
            {
                "AUTOSE_TRACE_PATH": f"/logs/agent/{self._TRACE_FILE}",
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = (
            f"cd {_WORKSPACE} && timeout -s INT -k 120 {self._wall_timeout_sec} "
            f"{_HOME}/pro/venv/bin/autose --json --yes --tier {shlex.quote(self._tier)} "
            f"--config {config_path} --workspace {_WORKSPACE} "
            f"-- {shlex.quote(instruction)} "
            f"2>/logs/agent/{_STDERR_FILE} </dev/null | tee /logs/agent/{self._OUTPUT_FILE}; "
            # Exit 1 is Pro's "did not complete", and 124 is the wall clock;
            # both still leave work to grade.
            "rc=${PIPESTATUS[0]}; [ $rc -le 1 ] || [ $rc -eq 124 ] || exit $rc"
        )
        try:
            await self.exec_as_agent(environment, command=f"bash -c {shlex.quote(command)}", env=env)
        finally:
            if self._commit_fallback:
                await environment.exec(
                    command=(
                        f"cd {_WORKSPACE} && git config --global --add safe.directory {_WORKSPACE}; "
                        "git add -A -- . ':(exclude).autose' && git -c user.name=autose -c user.email=autose@localhost "
                        "commit -q -m 'AutoSE: commit remaining changes' || true"
                    )
                )

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = Path(self.logs_dir) / self._TRACE_FILE
        if not path.exists():
            return
        prompt = completion = tools = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            prompt += int(record.get("prompt_tokens") or 0)
            completion += int(record.get("completion_tokens") or 0)
            tools += len(record.get("tool_calls") or [])
        context.n_input_tokens = prompt
        context.n_output_tokens = completion
        context.cost_usd = 0.0
        context.n_agent_steps = tools
