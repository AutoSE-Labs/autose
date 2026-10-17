"""Whether the context window a model is served in is a problem, and what to do.

DR-022 made the gap between a model's maximum window and the window the runtime
actually serves visible. Visible is not the same as actionable: reported the
same way whether the window is a crippling 4k or a deliberate 32k chosen to fit
a small GPU, and always with the same fixed remedy, the warning trains people to
ignore it. :func:`advise_context` turns the facts into one verdict with a level,
a reason, and a remedy computed for this model on this runtime (DR-034).

Pure functions over :class:`ModelInfo`: no I/O, so every adapter shows the same
verdict and the rules are pinned by plain unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from logic.inference.catalog import ModelInfo

__all__ = [
    "COMFORTABLE_CONTEXT_TOKENS",
    "ContextAdvice",
    "advise_context",
    "context_remedy",
]

#: A window in which every stage of `craft` and `forge` has room to read a few
#: files and still answer. Below it work is possible but evicts history often;
#: above it more room buys longer runs, not correctness.
COMFORTABLE_CONTEXT_TOKENS = 32_768

#: Below this share of the model resident on the GPU, a larger window is a
#: slower model rather than a better one: the KV cache grows on the same card
#: and pushes more layers onto the CPU.
_FULLY_ON_GPU = 0.999

Level = Literal["ok", "info", "warning", "blocking"]

#: How each runtime is told to serve a larger window. A provider without an
#: entry gets advice without a command rather than a wrong one.
_REMEDIES: dict[str, str] = {
    "ollama": "OLLAMA_CONTEXT_LENGTH={window} ollama serve",
    "vllm": "vllm serve <model> --max-model-len {window}",
    "llama-cpp": "llama-server -m <model.gguf> -c {window}",
    "lm-studio": "set Context Length to {window} in the model's load settings",
}


@dataclass(frozen=True)
class ContextAdvice:
    """One verdict about one model's served window."""

    level: Level
    served: int | None
    maximum: int | None
    #: The window worth moving to, or ``None`` when the current one should stay.
    recommended: int | None
    #: One line for a status bar or a banner.
    summary: str
    #: The reasoning behind the level, for whoever asks why.
    detail: str
    #: A runtime-specific way to serve ``recommended``; empty when there is none.
    remedy: str = ""

    @property
    def needs_attention(self) -> bool:
        """True for the levels a standing warning is worth showing for."""
        return self.level in ("warning", "blocking")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "served": self.served,
            "maximum": self.maximum,
            "recommended": self.recommended,
            "summary": self.summary,
            "detail": self.detail,
            "remedy": self.remedy,
        }


def context_remedy(provider: str, window: int) -> str:
    """How to serve ``window`` tokens on ``provider``, or ``""`` if unknown."""
    template = _REMEDIES.get(provider)
    return template.format(window=window) if template else ""


def advise_context(
    info: ModelInfo,
    *,
    provider: str = "",
    min_usable: int,
    comfortable: int = COMFORTABLE_CONTEXT_TOKENS,
) -> ContextAdvice:
    """Judge ``info``'s served window for AutoSE's needs (DR-034).

    ``min_usable`` is the floor below which the session refuses to run
    (``tuning.context.min_usable_context_tokens``), passed in so the advice and
    the refusal can never disagree about where it is.
    """
    served = info.context_length
    maximum = info.max_context_length
    if served is None:
        return ContextAdvice(
            level="ok",
            served=None,
            maximum=maximum,
            recommended=None,
            summary="The runtime does not report its context window.",
            detail="AutoSE budgets against the configured context_limit instead.",
        )

    ceiling = maximum or max(served, comfortable)
    gpu = info.gpu_fraction
    on_cpu = gpu is not None and gpu < _FULLY_ON_GPU
    spill = (
        f" {round((1 - gpu) * 100)}% of the model already runs on the CPU, so a "
        "larger window will also be slower."
        if on_cpu and gpu is not None
        else ""
    )
    assumed = (
        ""
        if info.context_length_measured
        else " (assumed: the model is not loaded, so the runtime has not said)"
    )

    if served < min_usable:
        target = min(comfortable, ceiling)
        return ContextAdvice(
            level="blocking",
            served=served,
            maximum=maximum,
            recommended=target,
            summary=(
                f"{served:,} tokens{assumed} is below the {min_usable:,} AutoSE "
                "needs to work."
            ),
            detail=(
                "Tool schemas and the stage prompt fill most of the window before "
                "the request is counted, so each round would evict the files the "
                f"previous one read.{spill}"
            ),
            remedy=context_remedy(provider, target),
        )

    if served < comfortable and served < ceiling:
        target = min(comfortable, ceiling)
        return ContextAdvice(
            level="warning",
            served=served,
            maximum=maximum,
            recommended=target,
            summary=(
                f"{served:,} tokens{assumed} works, but multi-stage runs will "
                "keep dropping earlier reads."
            ),
            detail=(
                f"{target:,} gives every craft and forge stage room to read and "
                f"still answer.{spill}"
            ),
            remedy=context_remedy(provider, target),
        )

    if maximum is not None and served < maximum:
        if on_cpu:
            return ContextAdvice(
                level="info",
                served=served,
                maximum=maximum,
                recommended=None,
                summary=(
                    f"Serving {served:,} of the model's {maximum:,} tokens — "
                    "enough for AutoSE, and the right cap for this machine."
                ),
                detail=(
                    "AutoSE budgets every request to fit the served window, so "
                    f"nothing is silently dropped.{spill} Keep it."
                ),
            )
        larger = min(served * 2, maximum)
        return ContextAdvice(
            level="info",
            served=served,
            maximum=maximum,
            recommended=larger,
            summary=(
                f"Serving {served:,} of the model's {maximum:,} tokens — enough "
                "for AutoSE."
            ),
            detail=(
                "AutoSE budgets every request to fit the served window, so nothing "
                "is silently dropped. The model fits on the GPU, so a larger "
                "window is an option for longer runs if memory allows."
            ),
            remedy=context_remedy(provider, larger),
        )

    return ContextAdvice(
        level="ok",
        served=served,
        maximum=maximum,
        recommended=None,
        summary=f"Serving {served:,} tokens{assumed}.",
        detail="The full window the model supports, or more than AutoSE needs.",
    )
