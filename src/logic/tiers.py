"""The single source of truth for complexity tiers (DR-003).

Tiers are ordered least to most complex. Everything that needs to know what
tiers exist derives it from here — the classifier's valid answers, the CLI's
choices, the workflow registry's keys, and the session ratchet — so adding or
renaming one is a change in this file, not a hunt through the codebase.

The names are a spectrum of effort, not severity labels: ``spark`` for a
question or a one-line change, ``craft`` for everything that needs a plan,
from ordinary feature and bug work to changes spanning many files.

The Pro edition adds ``forge``, a Design stage that writes a decision record,
on top of these two. The free edition has no such tier; a ``forge`` name read
from elsewhere (a Pro session file, a stale config) normalises to the default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "SPARK",
    "CRAFT",
    "TIERS",
    "TIER_RANK",
    "DEFAULT_TIER",
    "TierSpec",
    "TIER_SPECS",
    "is_tier",
    "normalise_tier",
    "ratchet",
    "describe_tier",
]

SPARK = "spark"
CRAFT = "craft"

#: Ordered least to most complex. Order *is* the rank.
TIERS: tuple[str, ...] = (SPARK, CRAFT)

#: Rank by position, so a session can compare two tiers and never downgrade.
TIER_RANK: Mapping[str, int] = {tier: index for index, tier in enumerate(TIERS)}

#: Where classification lands when it fails outright. DR-003 chooses the
#: middle tier deliberately: guessing high is expensive, guessing low
#: under-powers the request.
DEFAULT_TIER = CRAFT


@dataclass(frozen=True)
class TierSpec:
    """What a tier means, for prompts, pickers, and help text."""

    name: str
    summary: str
    #: The stages this tier runs, in order. Empty for a single direct pass.
    stages: tuple[str, ...]
    #: One-line guidance the classifier is given for choosing this tier.
    classifier_hint: str

    @property
    def rank(self) -> int:
        return TIER_RANK[self.name]

    @property
    def is_multi_stage(self) -> bool:
        return len(self.stages) > 1


TIER_SPECS: Mapping[str, TierSpec] = {
    SPARK: TierSpec(
        name=SPARK,
        summary="A single direct pass — answer a question, or make one small edit.",
        stages=(),
        classifier_hint=(
            "a question about the code, or a change small enough to need no write "
            "or only a trivial one (a single line, one value or config change, a comment)"
        ),
    ),
    CRAFT: TierSpec(
        name=CRAFT,
        summary="Plan then Execute then Validate — the everyday pipeline.",
        stages=("plan", "execute", "validate"),
        classifier_hint=(
            "implement a feature or fix a bug, refactor, or any change that needs a "
            "plan, whether it touches a handful of files or many"
        ),
    ),
}


def is_tier(value: str) -> bool:
    return value in TIER_RANK


def normalise_tier(value: str, *, fallback: str = DEFAULT_TIER) -> str:
    """Map free text onto a tier name, falling back rather than raising."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in TIER_RANK else fallback


def ratchet(current: str | None, proposed: str) -> str:
    """Return the higher of two tiers — the session ratchet (DR-003).

    A complex, multi-turn task contains simple-looking messages ("yes, do
    that") that read as trivial in isolation. Re-classifying those downward
    would drop the pipeline mid-flight along with the state it depends on, so
    the effective tier only ever moves up within a session.
    """
    proposed = normalise_tier(proposed)
    if current is None or current not in TIER_RANK:
        return proposed
    return current if TIER_RANK[current] >= TIER_RANK[proposed] else proposed


def describe_tier(tier: str) -> str:
    spec = TIER_SPECS.get(normalise_tier(tier))
    return spec.summary if spec else ""
