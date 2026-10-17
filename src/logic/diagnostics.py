"""Environment diagnostics as data (DR-002).

Answers "what can this machine actually run?" and returns the answer as a
structure. It does not print anything: an Adapter renders it — the terminal as
a table, a GUI as a panel — from the same computation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .common.context import MIN_USABLE_CONTEXT_TOKENS
from .config import AutoSEConfig, load_config
from .errors import AutoSEError
from .inference.discovery import DetectedRuntime, discover_local_runtimes
from .inference.ollama import OLLAMA_CONTEXT_ENV
from .workflows import available_tiers

__all__ = ["Diagnosis", "diagnose"]


@dataclass(frozen=True)
class Diagnosis:
    """A snapshot of what AutoSE found, and what it would do about it."""

    config: AutoSEConfig
    runtimes: tuple[DetectedRuntime, ...] = ()
    implemented_tiers: tuple[str, ...] = ()
    problems: tuple[str, ...] = field(default_factory=tuple)
    #: Things worth telling the user that do not make the setup unusable. A
    #: context window smaller than the model supports is the motivating case
    #: (DR-022): everything works, just in less room than the user assumes,
    #: and nothing else would reveal it.
    advisories: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable_runtimes(self) -> tuple[DetectedRuntime, ...]:
        return tuple(runtime for runtime in self.runtimes if runtime.usable)

    @property
    def healthy(self) -> bool:
        return bool(self.usable_runtimes) and not self.problems

    @property
    def total_models(self) -> int:
        return sum(len(runtime.models) for runtime in self.usable_runtimes)


def diagnose(config: AutoSEConfig | None = None) -> Diagnosis:
    """Probe the environment. Never raises — every failure becomes a problem."""
    problems: list[str] = []
    if config is None:
        try:
            config = load_config()
        except AutoSEError as exc:
            problems.append(str(exc))
            config = AutoSEConfig()

    runtimes = discover_local_runtimes(
        timeout=config.inference.discovery_timeout, include_unreachable=True
    )
    if not any(runtime.usable for runtime in runtimes):
        problems.append(
            "no local runtime is serving a model. Start one (`ollama serve`) and "
            "pull a model (`ollama pull qwen3:8b`), or configure a remote "
            "endpoint with AUTOSE_BASE_URL."
        )

    tool_incapable = [
        f"{runtime.provider}/{model.name}"
        for runtime in runtimes
        for model in runtime.models
        if model.supports_tools is False
    ]
    if tool_incapable and not _any_tool_capable(runtimes):
        problems.append(
            "no available model reports tool-calling support. AutoSE needs tools "
            f"to read and change files. Models found: {', '.join(tool_incapable)}."
        )

    return Diagnosis(
        config=config,
        runtimes=runtimes,
        implemented_tiers=available_tiers(),
        problems=tuple(problems),
        advisories=_context_advisories(runtimes),
    )


def _context_advisories(runtimes: tuple[DetectedRuntime, ...]) -> tuple[str, ...]:
    """Report every model being served in less room than it supports (DR-022).

    Reported per runtime rather than per model: the cap is a property of how
    the runtime was started, so naming each affected model separately would
    repeat one fact once per model.
    """
    advisories: list[str] = []
    for runtime in runtimes:
        too_small = [
            model
            for model in runtime.models
            if model.context_length
            and model.context_length < MIN_USABLE_CONTEXT_TOKENS
        ]
        if too_small:
            smallest = min(model.context_length or 0 for model in too_small)
            advisories.append(
                f"{runtime.provider} is serving {smallest:,} tokens, below the "
                f"{MIN_USABLE_CONTEXT_TOKENS:,} AutoSE needs to work usefully — "
                "tool schemas and the stage prompt consume most of that before "
                "your request is counted."
            )
        capped = [model for model in runtime.models if model.is_context_capped]
        if not capped:
            continue
        smallest = min(model.context_length or 0 for model in capped)
        largest = max(model.max_context_length or 0 for model in capped)
        advisories.append(
            f"{runtime.provider} is serving a {smallest:,}-token window for "
            f"{len(capped)} model(s) that support up to {largest:,}. An "
            "over-long request is trimmed by the runtime, not refused. Raise it "
            f"with `{OLLAMA_CONTEXT_ENV}=32768 ollama serve`."
        )
    return tuple(advisories)


def _any_tool_capable(runtimes: tuple[DetectedRuntime, ...]) -> bool:
    return any(
        model.is_tool_capable for runtime in runtimes for model in runtime.models
    )
