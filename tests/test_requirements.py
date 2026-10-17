"""DR-005: the canonical requirements ledger."""

from __future__ import annotations

import pytest

from logic.requirements import (
    EXECUTE_STATUSES,
    PLAN_STATUSES,
    VALIDATE_STATUSES,
    Requirement,
    RequirementsFormatError,
    format_requirements,
    parse_requirements,
    require_same_ids,
    strip_requirements_section,
    summarise,
    unverified_ids,
)

_PLAN = """\
Here is the plan. We will change two files.

## REQUIREMENTS STATUS
- R1 [Not Started] — Add a retry helper to net.py
- R2 [Not Started] — Cover the helper with a unit test
"""


def test_a_well_formed_plan_ledger_parses():
    parsed = parse_requirements(_PLAN, allowed_statuses=PLAN_STATUSES)
    assert [item.id for item in parsed] == ["R1", "R2"]
    assert parsed[0].description == "Add a retry helper to net.py"
    assert parsed[0].status == "Not Started"


def test_evidence_is_separated_from_the_description():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS\n- R1 [Implemented] — Add retry — Evidence: net.py:42",
        allowed_statuses=EXECUTE_STATUSES,
    )
    assert parsed[0].description == "Add retry"
    assert parsed[0].evidence == "net.py:42"


def test_a_missing_section_is_a_format_error():
    with pytest.raises(RequirementsFormatError, match="Missing"):
        parse_requirements("just prose", allowed_statuses=PLAN_STATUSES)


def test_an_empty_section_is_a_format_error():
    with pytest.raises(RequirementsFormatError, match="empty"):
        parse_requirements("## REQUIREMENTS STATUS\n\n", allowed_statuses=PLAN_STATUSES)


def test_a_malformed_line_is_a_format_error():
    with pytest.raises(RequirementsFormatError, match="Malformed"):
        parse_requirements(
            "## REQUIREMENTS STATUS\n- R1 add a thing", allowed_statuses=PLAN_STATUSES
        )


def test_a_duplicate_id_is_a_format_error():
    with pytest.raises(RequirementsFormatError, match="Duplicate"):
        parse_requirements(
            "## REQUIREMENTS STATUS\n- R1 [Not Started] — a\n- R1 [Not Started] — b",
            allowed_statuses=PLAN_STATUSES,
        )


def test_a_requirement_with_no_description_is_refused():
    with pytest.raises(RequirementsFormatError, match="Malformed"):
        parse_requirements(
            "## REQUIREMENTS STATUS\n- R1 [Not Started] —   ",
            allowed_statuses=PLAN_STATUSES,
        )


def test_each_stage_may_only_use_its_own_status_vocabulary():
    """'Implemented' must not be mistakable for 'Verified'."""
    with pytest.raises(RequirementsFormatError, match="may only use"):
        parse_requirements(
            "## REQUIREMENTS STATUS\n- R1 [Verified] — done",
            allowed_statuses=EXECUTE_STATUSES,
        )


def test_status_case_is_normalised_to_the_canonical_spelling():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS\n- R1 [implemented] — a", allowed_statuses=EXECUTE_STATUSES
    )
    assert parsed[0].status == "Implemented"


@pytest.mark.parametrize("dash", ["—", "–", "-"])
def test_any_dash_separator_is_accepted(dash):
    parsed = parse_requirements(
        f"## REQUIREMENTS STATUS\n- R1 [Not Started] {dash} a thing",
        allowed_statuses=PLAN_STATUSES,
    )
    assert parsed[0].description == "a thing"


@pytest.mark.parametrize("bullet", ["-", "*"])
def test_either_bullet_marker_is_accepted(bullet):
    parsed = parse_requirements(
        f"## REQUIREMENTS STATUS\n{bullet} R1 [Not Started] — a thing",
        allowed_statuses=PLAN_STATUSES,
    )
    assert parsed[0].id == "R1"


def test_the_last_section_wins_when_an_incoming_ledger_is_quoted():
    text = (
        "## REQUIREMENTS STATUS\n- R1 [Not Started] — old\n\n"
        "Some work happened.\n\n"
        "## REQUIREMENTS STATUS\n- R1 [Implemented] — new — Evidence: x.py"
    )
    parsed = parse_requirements(text, allowed_statuses=EXECUTE_STATUSES)
    assert parsed[0].status == "Implemented"
    assert parsed[0].description == "new"


def test_parsing_stops_at_the_verdict_line():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS\n- R1 [Verified] — a\nVERDICT: PASS",
        allowed_statuses=VALIDATE_STATUSES,
    )
    assert len(parsed) == 1


def test_a_following_heading_ends_the_section():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS\n- R1 [Not Started] — a\n\n## Notes\n- not a requirement",
        allowed_statuses=PLAN_STATUSES,
    )
    assert len(parsed) == 1


# -- tolerance of bold markdown (DR-017) --------------------------------------


def test_a_bolded_requirement_id_is_accepted():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS\n- **R1** [Verified] — a — Evidence: x.py",
        allowed_statuses=VALIDATE_STATUSES,
    )
    assert parsed[0].id == "R1"


def test_a_bolded_status_bracket_is_accepted():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS\n- R1 **[Verified]** — a — Evidence: x.py",
        allowed_statuses=VALIDATE_STATUSES,
    )
    assert parsed[0].status == "Verified"


def test_a_bolded_heading_is_accepted():
    parsed = parse_requirements(
        "## **REQUIREMENTS STATUS**\n- R1 [Not Started] — a",
        allowed_statuses=PLAN_STATUSES,
    )
    assert parsed[0].id == "R1"


def test_a_heading_with_a_trailing_colon_is_accepted():
    parsed = parse_requirements(
        "## REQUIREMENTS STATUS:\n- R1 [Not Started] — a",
        allowed_statuses=PLAN_STATUSES,
    )
    assert parsed[0].id == "R1"


# -- the same-IDs contract ----------------------------------------------------


def _reqs(*specs):
    return [Requirement(id=i, description=d, status=s) for i, d, s in specs]


def test_identical_ids_pass():
    planned = _reqs(("R1", "a", "Not Started"), ("R2", "b", "Not Started"))
    done = _reqs(("R2", "b", "Implemented"), ("R1", "a", "Implemented"))
    require_same_ids(planned, done)


def test_a_dropped_requirement_is_caught():
    planned = _reqs(("R1", "a", "Not Started"), ("R2", "b", "Not Started"))
    with pytest.raises(RequirementsFormatError, match="dropped R2"):
        require_same_ids(planned, _reqs(("R1", "a", "Implemented")))


def test_an_invented_requirement_is_caught():
    planned = _reqs(("R1", "a", "Not Started"))
    with pytest.raises(RequirementsFormatError, match="invented R9"):
        require_same_ids(
            planned, _reqs(("R1", "a", "Implemented"), ("R9", "x", "Implemented"))
        )


# -- rendering ------------------------------------------------------------------


def test_formatting_round_trips_through_parsing():
    planned = parse_requirements(_PLAN, allowed_statuses=PLAN_STATUSES)
    reparsed = parse_requirements(
        format_requirements(planned), allowed_statuses=PLAN_STATUSES
    )
    assert reparsed == planned


def test_evidence_survives_a_round_trip():
    original = _reqs(("R1", "a", "Implemented"))
    original[0] = Requirement("R1", "a", "Implemented", "src/x.py:1")
    reparsed = parse_requirements(
        format_requirements(original), allowed_statuses=EXECUTE_STATUSES
    )
    assert reparsed[0].evidence == "src/x.py:1"


def test_stripping_removes_the_ledger_and_keeps_the_prose():
    stripped = strip_requirements_section(_PLAN)
    assert "REQUIREMENTS STATUS" not in stripped
    assert "Here is the plan" in stripped


def test_stripping_keeps_a_following_section():
    text = "Intro\n\n## REQUIREMENTS STATUS\n- R1 [Not Started] — a\n\n## Notes\nkeep me"
    stripped = strip_requirements_section(text)
    assert "keep me" in stripped and "R1 [Not Started]" not in stripped


def test_stripping_text_without_a_ledger_is_a_no_op():
    assert strip_requirements_section("plain text") == "plain text"


def test_unverified_ids_lists_everything_not_verified():
    items = _reqs(("R1", "a", "Verified"), ("R2", "b", "Failed"), ("R3", "c", "Verified"))
    assert unverified_ids(items) == ["R2"]


def test_summarise_counts_by_status():
    assert "2 requirement(s)" in summarise(
        _reqs(("R1", "a", "Verified"), ("R2", "b", "Failed"))
    )
    assert summarise([]) == "no requirements"
