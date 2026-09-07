from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Literal, Union

from core.runners import LiteRunner, StandardRunner
from core.session import TaskSessionRecorder

_CODE_DIR = Path(__file__).resolve().parents[2]
_LOGIC_DIR = _CODE_DIR / "logic"
if str(_LOGIC_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGIC_DIR))

Mode = Literal["lite", "standard"]


def _cached_tokens(usage: dict) -> int:
    """Return cached prompt tokens from OpenAI-compat usage, else 0."""
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details.get("cached_tokens") or 0)
    for key in ("cached_tokens", "cache_tokens", "num_cached_tokens"):
        if usage.get(key) is not None:
            return int(usage.get(key) or 0)
    return 0


def _metrics_url(base_url: str) -> str:
    value = (base_url or "").rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3]
    return f"{value.rstrip('/')}/metrics"


def _prefix_cache_from_metrics(text: str) -> dict[str, float]:
    hits = None
    queries = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split()[0]
        if name == "vllm:prefix_cache_hits_total":
            hits = float(line.rsplit(None, 1)[-1])
        elif name == "vllm:prefix_cache_queries_total":
            queries = float(line.rsplit(None, 1)[-1])
    out: dict[str, float] = {}
    if hits is not None:
        out["vllm_prefix_cache_hits_total"] = hits
    if queries is not None:
        out["vllm_prefix_cache_queries_total"] = queries
    return out


def _fetch_prefix_cache(base_url: str) -> dict[str, float]:
    try:
        with urllib.request.urlopen(_metrics_url(base_url), timeout=2) as resp:
            return _prefix_cache_from_metrics(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {}


class HeadlessClient:
    """Client-neutral runner adapter for JSON/stdout consumers."""

    def __init__(
        self,
        recorder: TaskSessionRecorder,
        *,
        auto_approve_commands: bool = False,
        stream: bool = False,
        event_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self.recorder = recorder
        self.auto_approve_commands = auto_approve_commands
        self.stream = stream
        self.event_sink = event_sink
        self._emitted_event_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.energy_joules = 0.0
        self.energy_quality = "unavailable"
        self.energy_scope = "none"
        self.energy_display = ""
        self.energy_calls = 0
        self.messages: list[dict] = []
        self.vllm_prefix_cache_hits_total = 0.0
        self.vllm_prefix_cache_queries_total = 0.0
        self._flush_events()

    def _flush_events(self) -> None:
        if not self.event_sink:
            self._emitted_event_count = len(self.recorder.events)
            return
        for event in self.recorder.events[self._emitted_event_count :]:
            self.event_sink(asdict(event))
        self._emitted_event_count = len(self.recorder.events)

    def _emit(self, event_type: str, message: str = "", **data) -> None:
        self.recorder.emit(event_type, message, **data)
        self._flush_events()

    def prepare_agent(self, agent: object) -> None:
        from energy.format import format_joules
        from energy.tracking import install_energy_tracking, result_to_event_data

        def on_energy(result) -> None:
            if isinstance(result.energy_joules, (int, float)):
                self.energy_joules += float(result.energy_joules)
            self.energy_quality = result.quality
            self.energy_scope = result.scope
            self.energy_calls += 1
            if self.energy_quality == "approximated":
                self.energy_display = f"~{format_joules(self.energy_joules)} · approx"
            elif self.energy_scope == "gpu":
                self.energy_display = f"{format_joules(self.energy_joules)} · GPU"
            elif self.energy_scope == "cpu_package":
                self.energy_display = f"{format_joules(self.energy_joules)} · CPU"
            else:
                self.energy_display = format_joules(self.energy_joules)
            payload = result_to_event_data(result)
            self._emit(
                "energy_updated",
                message=payload["display"],
                total_energy_joules=self.energy_joules,
                total_display=self.energy_display,
                call_count=self.energy_calls,
                **payload,
            )

        install_energy_tracking(agent, on_result=on_energy)
        original_call = agent._call_sync

        def tracked_call(messages, tools=None, **kwargs):
            result = original_call(messages, tools=tools, **kwargs)
            usage = result.get("usage")
            if usage:
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
                self.cached_tokens += _cached_tokens(usage)
                cache_metrics = _fetch_prefix_cache(getattr(agent, "_base_url", "") or "")
                if cache_metrics:
                    self.vllm_prefix_cache_hits_total = cache_metrics.get(
                        "vllm_prefix_cache_hits_total", self.vllm_prefix_cache_hits_total
                    )
                    self.vllm_prefix_cache_queries_total = cache_metrics.get(
                        "vllm_prefix_cache_queries_total",
                        self.vllm_prefix_cache_queries_total,
                    )
                payload = {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "cached_tokens": self.cached_tokens,
                    "total_tokens": self.prompt_tokens + self.completion_tokens,
                    "vllm_prefix_cache_hits_total": self.vllm_prefix_cache_hits_total,
                    "vllm_prefix_cache_queries_total": self.vllm_prefix_cache_queries_total,
                }
                self._emit("tokens_updated", **payload)
                usage_path = os.environ.get("AUTOSE_USAGE_PATH")
                if usage_path:
                    Path(usage_path).write_text(
                        json.dumps(payload) + "\n",
                        encoding="utf-8",
                    )
            return result

        agent._call_sync = tracked_call  # type: ignore[method-assign]

        original_stream = getattr(agent, "_call_stream", None)
        if original_stream is not None:
            def tracked_stream(messages, tools=None, **kwargs):
                # Route the final answer through tracked _call_sync so usage is counted.
                response = tracked_call(messages, tools=tools, **kwargs)
                content = (
                    ((response.get("choices") or [{}])[0].get("message") or {}).get("content")
                    or ""
                )
                if content:
                    yield content
                else:
                    yield from original_stream(messages, tools=tools)

            agent._call_stream = tracked_stream  # type: ignore[method-assign]

        original_execute = agent._execute_tool

        def tracked_execute(tool_call: dict) -> str:
            name = tool_call["function"]["name"]
            try:
                args = json.loads(tool_call["function"].get("arguments", "{}"))
            except Exception:
                args = {}

            if name == "run_command" and "run_command" in getattr(agent, "_tools", {}):
                command = args.get("command", "")
                self._emit(
                    "approval_requested",
                    tool=name,
                    command=command,
                )
                self._emit(
                    "approval_resolved",
                    tool=name,
                    command=command,
                    allowed=self.auto_approve_commands,
                )
                if not self.auto_approve_commands:
                    return "Command was denied by the headless approval policy."

            self._emit("tool_called", tool=name, arguments=args)
            return original_execute(tool_call)

        agent._execute_tool = tracked_execute  # type: ignore[method-assign]

    def build_session_context(self, call_fn) -> list[dict] | None:
        return None

    def record_memory_exchange(self, prompt: str, assistant: str) -> None:
        self._emit("memory_exchange_recorded", prompt=prompt)

    def start_thinking(self, label: str) -> None:
        self._emit("agent_stage_started", stage="lite", label=label)

    def start_stage(
        self,
        stage: str,
        title: str,
        thinking_label: str,
        **data,
    ) -> None:
        self._emit(
            "stage_started",
            stage=stage,
            title=title,
            thinking_label=thinking_label,
            **data,
        )

    def stream_assistant_chunk(self, chunk: str) -> None:
        if self.stream:
            print(chunk, end="", flush=True)
        self.messages.append({"role": "assistant_chunk", "content": chunk})
        self._emit("assistant_chunk", content=chunk)

    def finish_streaming(self) -> None:
        self._emit("stream_finished")

    def add_assistant_message(self, message: str) -> None:
        if self.stream:
            print(message, flush=True)
        self.messages.append({"role": "assistant", "content": message})
        self._emit("assistant_message_added", content=message)

    def add_user_message(self, message: str) -> None:
        self.messages.append({"role": "user", "content": message})
        self._emit("user_message_added", content=message)

    def request_plan_review(self) -> str:
        self._emit(
            "approval_requested",
            kind="plan_review",
        )
        self._emit(
            "approval_resolved",
            kind="plan_review",
            approved=True,
            feedback="",
        )
        return ""

    def capture_file_update(self, path: str, old_text: str, new_text: str) -> None:
        self.recorder.note_changed_file(path)
        self._flush_events()
        self.recorder.add_artifact(
            "diff",
            f"Updated {Path(path).name}",
            path=path,
            content=new_text,
            old_text=old_text,
        )
        self._flush_events()

    def add_artifact(
        self,
        kind: str,
        title: str,
        *,
        path: str = "",
        content: str = "",
        **metadata,
    ) -> None:
        self.recorder.add_artifact(
            kind,
            title,
            path=path,
            content=content,
            **metadata,
        )
        self._flush_events()

    def note_test(self, name: str, status: str, details: str = "") -> None:
        self.recorder.note_test(name, status, details)
        self._flush_events()

    def complete(self, summary: str) -> None:
        self.recorder.complete(summary)
        self._flush_events()

    def fail(self, message: str) -> None:
        self.recorder.fail(message)
        self._flush_events()

    def reset_activity(self) -> None:
        self._emit("client_reset")

    def to_dict(self) -> dict:
        return {
            "session_id": self.recorder.session_id,
            "task": self.recorder.task,
            "workspace_root": self.recorder.workspace_root,
            "mode": self.recorder.mode,
            "created_at": self.recorder.created_at,
            "events": [asdict(event) for event in self.recorder.events],
            "artifacts": [asdict(artifact) for artifact in self.recorder.artifacts],
            "result": asdict(self.recorder.result),
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cached_tokens": self.cached_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "vllm_prefix_cache_hits_total": self.vllm_prefix_cache_hits_total,
                "vllm_prefix_cache_queries_total": self.vllm_prefix_cache_queries_total,
                "energy_joules": self.energy_joules,
                "energy_quality": self.energy_quality,
                "energy_scope": self.energy_scope,
                "energy_display": self.energy_display,
                "energy_calls": self.energy_calls,
            },
            "messages": self.messages,
        }


def run_headless(
    prompt: str,
    config_path: Union[str, Path],
    workspace_root: Union[str, Path],
    *,
    mode: Mode,
    auto_approve_commands: bool = False,
    stream: bool = False,
    event_sink: Callable[[dict], None] | None = None,
) -> dict:
    recorder = TaskSessionRecorder(
        task=prompt,
        workspace_root=str(workspace_root),
        mode=mode,
    )
    client = HeadlessClient(
        recorder,
        auto_approve_commands=auto_approve_commands,
        stream=stream,
        event_sink=event_sink,
    )

    runner_by_mode = {
        "lite": LiteRunner,
        "standard": StandardRunner,
    }
    runner_by_mode[mode](
        config_path=config_path,
        workspace_root=workspace_root,
        client=client,
    ).run(prompt)
    return client.to_dict()
