"""The canonical requirements ledger (DR-005).

In a multi-stage workflow the final "this is done" signal is only worth as much
as what the verifying stage actually checked. A stage that free-forms "looks
good" against a plan it may not remember precisely is not a quality gate. So
the planning stage emits a small, strictly formatted section of uniquely IDed
requirements, and every downstream stage echoes back **exactly those IDs** —
never inventing, dropping, splitting, or renaming one — updating only the
status and attaching evidence.

This module parses that section with deterministic code. No extra model call:
a fixed format is something a parser handles reliably, and an LLM extraction
step would add latency, cost, and a failure surface of its own.

A structurally invalid ledger is a stage failure, not something to tolerate —
it blocks that stage's output from becoming canonical state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "Requirement",
    "RequirementsFormatError",
    "PLAN_STATUSES",
    "EXECUTE_STATUSES",
    "VALIDATE_STATUSES",
    "PLAN_LEDGER_INSTRUCTION",
    "EXECUTE_LEDGER_INSTRUCTION",
    "VALIDATE_LEDGER_INSTRUCTION",
    "parse_requirements",
    "require_same_ids",
    "as_payload",
    "format_requirements",
    "strip_requirements_section",
    "unverified_ids",
    "summarise",
]


class RequirementsFormatError(ValueError):
    """A stage artifact violated the ledger contract."""


@dataclass(frozen=True)
class Requirement:
    """One independently verifiable requirement, at one point in the pipeline."""

    id: str
    description: str
    status: str
    evidence: str = ""

    @property
    def is_verified(self) -> bool:
        return self.status == "Verified"

    def render(self) -> str:
        line = f"- {self.id} [{self.status}] — {self.description}"
        return f"{line} — Evidence: {self.evidence}" if self.evidence else line


#: Statuses each stage may use. A stage using another stage's vocabulary is a
#: format error, which keeps "Implemented" from being mistaken for "Verified".
PLAN_STATUSES = frozenset({"Not Started"})
EXECUTE_STATUSES = frozenset({"Implemented", "In Progress", "Blocked"})
#: `Unverified` is a requirement the validator could not get a check to run
#: for — distinct from `Failed`, which reports a defect it observed. Lumping
#: the two together sent work back to execute with nothing to fix.
VALIDATE_STATUSES = frozenset({"Verified", "Failed", "Unverified"})

_HEADING = re.compile(
    r"^#{1,6}\s*\**\s*REQUIREMENTS STATUS\s*\**\s*:?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_NEXT_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
# The separator accepts an em dash, en dash, or hyphen: which one a model emits
# carries no meaning, and rejecting two of the three fails stages over
# typography rather than over substance.
_SEPARATOR = r"[—–-]"
# ``**`` around the ID and/or the status bracket is tolerated the same way: a
# 12B model bolding a ledger line is presentation variance, not a format error
# (DR-017).
_ITEM = re.compile(
    rf"^\s*[-*]\s+\**(?P<id>R[0-9A-Za-z._-]*)\**\s*\**\[(?P<status>[^\]]+)\]\**\s*"
    rf"{_SEPARATOR}\s+(?P<body>.+?)\s*$"
)
_EVIDENCE = re.compile(rf"\s+{_SEPARATOR}\s+Evidence:\s*(?P<evidence>.+)$", re.IGNORECASE)


def parse_requirements(
    text: str, *, allowed_statuses: Iterable[str]
) -> list[Requirement]:
    """Parse the final ``REQUIREMENTS STATUS`` section of a stage artifact.

    The *last* such section wins: a stage that quotes the incoming ledger
    before emitting its own updated one is following the prompt, not breaking
    the contract.
    """
    headings = list(_HEADING.finditer(text or ""))
    if not headings:
        raise RequirementsFormatError(
            "Missing the '## REQUIREMENTS STATUS' section. End the response with "
            "exactly one such section, one line per requirement."
        )

    start = headings[-1].end()
    next_heading = _NEXT_HEADING.search(text, start)
    section = text[start : next_heading.start() if next_heading else len(text)]

    canonical = {status.casefold(): status for status in allowed_statuses}
    requirements: list[Requirement] = []
    seen: set[str] = set()

    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _ITEM.fullmatch(line)
        if not match:
            if requirements:
                # The ledger has ended. A closing sentence, or the VERDICT
                # line, is normal and harmless — and rejecting it would cost a
                # full stage retry to gain nothing. Anything genuinely lost
                # here is still caught downstream by require_same_ids(), which
                # is what actually enforces the contract.
                break
            raise RequirementsFormatError(
                f"Malformed ledger line: {line!r}. Expected "
                "'- R1 [Status] — description'."
            )

        requirement_id = match.group("id")
        if requirement_id in seen:
            raise RequirementsFormatError(f"Duplicate requirement ID: {requirement_id}")

        # Case is normalised rather than enforced: 'implemented' and
        # 'Implemented' mean the same thing, and failing a stage over the
        # difference costs a full retry to gain nothing.
        status = canonical.get(match.group("status").strip().casefold())
        if status is None:
            allowed = ", ".join(sorted(canonical.values()))
            raise RequirementsFormatError(
                f"Requirement {requirement_id} has status "
                f"'{match.group('status').strip()}'; this stage may only use: {allowed}."
            )

        # `body` is guaranteed non-empty and to start with a non-space
        # character by the item pattern, and the evidence pattern requires
        # leading whitespace — so the description can never come out empty.
        body = match.group("body").strip()
        evidence_match = _EVIDENCE.search(body)
        evidence = evidence_match.group("evidence").strip() if evidence_match else ""
        description = (body[: evidence_match.start()] if evidence_match else body).strip()

        requirements.append(
            Requirement(
                id=requirement_id,
                description=description,
                status=status,
                evidence=evidence,
            )
        )
        seen.add(requirement_id)

    if not requirements:
        raise RequirementsFormatError(
            "The requirements ledger is empty. List at least one requirement."
        )
    return requirements


def require_same_ids(
    expected: Sequence[Requirement], actual: Sequence[Requirement]
) -> None:
    """Require a downstream ledger to carry exactly the planned IDs.

    This is what makes the ledger a contract rather than a suggestion: a stage
    cannot quietly drop a requirement it failed to meet, nor invent one it did.
    """
    expected_ids = {item.id for item in expected}
    actual_ids = {item.id for item in actual}
    if expected_ids == actual_ids:
        return
    problems: list[str] = []
    missing = sorted(expected_ids - actual_ids)
    added = sorted(actual_ids - expected_ids)
    if missing:
        problems.append(f"dropped {', '.join(missing)}")
    if added:
        problems.append(f"invented {', '.join(added)}")
    raise RequirementsFormatError(
        "The requirement IDs changed: "
        + "; ".join(problems)
        + ". Echo back exactly the IDs you were given, updating only status and evidence."
    )


def format_requirements(requirements: Sequence[Requirement]) -> str:
    """Render the canonical ledger for injection into the next stage."""
    return "\n".join(
        ["## REQUIREMENTS STATUS", *(item.render() for item in requirements)]
    )


def strip_requirements_section(text: str) -> str:
    """Drop the trailing ledger, when a canonical parsed copy is injected instead.

    Keeps the artifact from carrying a second, potentially stale copy of state
    that :class:`RequestMemory` already owns (DR-004).
    """
    headings = list(_HEADING.finditer(text or ""))
    if not headings:
        return text
    heading = headings[-1]
    next_heading = _NEXT_HEADING.search(text, heading.end())
    if next_heading:
        return (text[: heading.start()] + text[next_heading.start() :]).strip()
    return text[: heading.start()].rstrip()


def unverified_ids(requirements: Sequence[Requirement]) -> list[str]:
    """IDs that are not ``Verified`` — the reason a claimed pass gets overridden."""
    return [item.id for item in requirements if not item.is_verified]


def as_payload(requirements: Sequence[Requirement]) -> tuple[dict[str, str], ...]:
    """The ledger as plain data, for events, the session file and adapters (DR-038)."""
    return tuple(
        {
            "id": item.id,
            "status": item.status,
            "description": item.description,
            "evidence": item.evidence,
        }
        for item in requirements
    )


def summarise(requirements: Sequence[Requirement]) -> str:
    """A one-line count by status, for status lines and logs."""
    if not requirements:
        return "no requirements"
    counts: dict[str, int] = {}
    for item in requirements:
        counts[item.status] = counts.get(item.status, 0) + 1
    parts = [f"{count} {status.lower()}" for status, count in sorted(counts.items())]
    return f"{len(requirements)} requirement(s): " + ", ".join(parts)


PLAN_LEDGER_INSTRUCTION = """
REQUIREMENTS LEDGER (mandatory, exact format):
End your plan with exactly one `## REQUIREMENTS STATUS` section. One line per
independently verifiable requirement, in exactly this shape:

## REQUIREMENTS STATUS
- R1 [Not Started] — Short, checkable statement of one requirement
- R2 [Not Started] — Another one

Rules: IDs are R1, R2, R3… in order. Every requirement must be something a
reviewer could independently confirm by reading the code or running something.
Use `Not Started` for every line — you are planning, not implementing. Put no
prose inside this section.
"""

EXECUTE_LEDGER_INSTRUCTION = """
REQUIREMENTS LEDGER (mandatory, exact format):
End your summary with exactly one `## REQUIREMENTS STATUS` section, carrying
**exactly** the requirement IDs you were given — never add, drop, split, or
rename one. Update only the status and attach evidence:

## REQUIREMENTS STATUS
- R1 [Implemented] — Same description you were given — Evidence: src/thing.py:42
- R2 [Blocked] — Same description you were given — Evidence: why it is blocked

Allowed statuses: Implemented, In Progress, Blocked. Evidence is a file path, a
path with a line number, or a command you ran. Put no prose inside this section.
"""

VALIDATE_LEDGER_INSTRUCTION = """
REQUIREMENTS LEDGER (mandatory, exact format):
End your report with exactly one `## REQUIREMENTS STATUS` section, carrying
**exactly** the requirement IDs you were given — never add, drop, split, or
rename one — followed by a single VERDICT line:

## REQUIREMENTS STATUS
- R1 [Verified] — Same description you were given — Evidence: what you checked
- R2 [Failed] — Same description you were given — Evidence: what was wrong
- R3 [Unverified] — Same description you were given — Evidence: why no check ran

VERDICT: PASS
or
VERDICT: FAIL execute — one sentence saying what must change

Allowed statuses: Verified, Failed, Unverified. Mark `Verified` only for
something you actually confirmed by reading the file or running a command — not
because the implementation claimed it. `Failed` means you observed a defect;
`Unverified` means you could not get a check to run, which is never a pass. The VERDICT target is one of design, plan, or
execute.
"""
