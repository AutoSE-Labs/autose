"""Deriving a validation verdict, fail-closed (DR-005).

The verifying stage says what it found. It does **not** get to decide whether
the workflow is done — the runner decides that, by independently re-parsing the
ledger. A claimed pass with any requirement unverified becomes a loopback
carrying the specific unmet IDs, which is what makes "is this actually done"
objectively checkable rather than a matter of trusting the model's self-report.

The verdict is read from the report's own text plus the parsed ledger, with no
second model call. The reference implementation this replaces made a separate
JSON-mode request purely to restate a conclusion already present in the report
— a whole extra generation, which on local hardware is the single most
expensive thing available to spend, for information the runner has to re-derive
anyway.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..requirements import Requirement, unverified_ids

__all__ = ["Verdict", "LOOPBACK_TARGETS", "derive_verdict"]

#: Stages a verdict may send work back to. A validator may still name
#: `design` (the Pro edition's `forge` tier has that stage); a runner without
#: it falls back to Execute.
LOOPBACK_TARGETS: tuple[str, ...] = ("design", "plan", "execute")

_VERDICT_LINE = re.compile(
    r"^\s*VERDICT:\s*(?P<outcome>PASS|FAIL)\b\s*(?P<rest>.*)$",
    re.MULTILINE | re.IGNORECASE,
)
_TARGET = re.compile(r"\b(design|plan|execute)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """The runner's decision about a validation round."""

    passed: bool
    #: Which stage to re-enter when not passed.
    target: str = "execute"
    feedback: str = ""
    #: Requirement IDs that blocked a pass, when the runner overrode one.
    unmet: tuple[str, ...] = ()
    #: True when the stage claimed a pass and the runner refused it. Surfaced
    #: so a user can see the gate did something, not just that it failed.
    overridden: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        if self.passed:
            return "passed"
        if self.overridden:
            return (
                f"claimed pass overridden — unverified: {', '.join(self.unmet)}"
            )
        return f"sent back to {self.target}"


def derive_verdict(
    report: str,
    requirements: Sequence[Requirement],
    *,
    available_targets: Sequence[str] = LOOPBACK_TARGETS,
) -> Verdict:
    """Decide the outcome from the report text and the canonical ledger.

    The ledger is necessary but not sufficient: a validator may legitimately
    fail a round on quality or architecture grounds while every requirement is
    verified, so an explicit ``VERDICT: FAIL`` is honoured even then.
    """
    unmet = tuple(unverified_ids(requirements))
    claimed_pass, stated_target, stated_feedback = _read_verdict_line(report)
    target = _resolve_target(stated_target, available_targets)

    if unmet:
        # DR-005: a claimed pass is not accepted at face value.
        feedback = stated_feedback or (
            "These requirements are not verified: " + ", ".join(unmet)
        )
        return Verdict(
            passed=False,
            target=target,
            feedback=feedback,
            unmet=unmet,
            overridden=claimed_pass is True,
            issues=(f"unverified requirement(s): {', '.join(unmet)}",),
        )

    if claimed_pass is False:
        return Verdict(
            passed=False,
            target=target,
            feedback=stated_feedback or "The validator rejected this attempt.",
            issues=(stated_feedback,) if stated_feedback else (),
        )

    if claimed_pass is None:
        # Every requirement verified but no readable verdict line. Fail closed:
        # an unreadable gate is not a passed gate.
        return Verdict(
            passed=False,
            target=target,
            feedback=(
                "The validation report had no readable 'VERDICT: PASS' or "
                "'VERDICT: FAIL <stage>' line. End the report with exactly one."
            ),
            issues=("missing verdict line",),
        )

    return Verdict(passed=True)


def _read_verdict_line(report: str) -> tuple[bool | None, str, str]:
    """Return ``(claimed_pass, target, feedback)`` from the last VERDICT line."""
    matches = list(_VERDICT_LINE.finditer(report or ""))
    if not matches:
        return None, "", ""
    match = matches[-1]
    passed = match.group("outcome").upper() == "PASS"
    rest = match.group("rest").strip()
    target_match = _TARGET.search(rest)
    target = target_match.group(1).lower() if target_match else ""
    feedback = rest[target_match.end() :] if target_match else rest
    return passed, target, feedback.lstrip(" —–-:").strip()


def _resolve_target(stated: str, available: Sequence[str]) -> str:
    """Pick a loopback target this workflow actually has.

    A `craft` run has no Design stage, so a validator asking for one is routed
    to Execute (or the first stage there is) rather than crashing the runner.
    """
    if stated and stated in available:
        return stated
    if "execute" in available:
        return "execute"
    return available[0] if available else "execute"
