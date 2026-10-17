"""What "no limit" means, in one place.

DR-030 makes every cap AutoSE has unlimited by default and keeps the caps
themselves configurable, so the same question — "is this configured value a
real bound, or does it mean *don't stop*?" — is asked at a dozen call sites.
It is answered here rather than re-implemented in each of them, because a
sentinel that means one thing in ``tuning.py`` and another in the agent loop
is worse than no sentinel at all.

The written form is ``-1``: a config file says ``max_tool_rounds: -1`` and
means "run until the work is done or I stop you". Internally that becomes
``None``, which is what the enforcing code can most naturally branch on, and
what a bare "the user left it out" already looks like.

Zero is treated as unlimited for a *duration* only: a budget of no seconds is
not a budget, it is a run that could never start. For a *count* it is clamped
up to the minimum instead, since a stage allowed no attempts cannot answer.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Final

__all__ = [
    "UNLIMITED",
    "count_limit",
    "counted",
    "describe_limit",
    "duration_limit",
    "is_unlimited",
]

#: What a config file writes to turn a cap off. Any negative value reads the
#: same way, so ``-1`` is a convention to document rather than a magic number
#: to match exactly.
UNLIMITED: Final[int] = -1


def is_unlimited(value: float | None) -> bool:
    """Whether *value* means "no limit": ``None``, ``-1``, or any negative."""
    return value is None or value < 0


def count_limit(value: int | None, *, minimum: int = 1) -> int | None:
    """Normalise a configured count cap. ``None`` is returned for unlimited."""
    # `value is None` is spelled out rather than left to `is_unlimited` so a
    # type checker can narrow it before the `int()` below.
    if value is None or is_unlimited(value):
        return None
    return max(minimum, int(value))


def duration_limit(value: float | None) -> float | None:
    """Normalise a configured wall-clock cap. ``None`` is returned for unlimited."""
    if is_unlimited(value) or not value:
        return None
    return float(value)


def counted(limit: int | None) -> Iterator[int]:
    """A 1-based attempt counter that stops at *limit*, or never when unlimited.

    The endless case is the default now, so it is the iterator rather than the
    loop body that carries the difference — a caller writes one ``for`` either
    way instead of a bounded loop beside an unbounded one.
    """
    return itertools.islice(itertools.count(1), limit)


def describe_limit(value: float | None, unit: str = "") -> str:
    """How a cap reads in a message to a person: a number, or "unlimited"."""
    if is_unlimited(value):
        return "unlimited"
    number = f"{value:g}"
    return f"{number} {unit}".strip()
