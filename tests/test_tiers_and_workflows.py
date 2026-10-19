"""DR-003: the tier source of truth, the ratchet, and workflow dispatch."""

from __future__ import annotations

import pytest

from logic.tiers import (
    CRAFT,
    DEFAULT_TIER,
    FORGE,
    SPARK,
    TIER_RANK,
    TIER_SPECS,
    TIERS,
    is_tier,
    normalise_tier,
    ratchet,
)
from logic.workflows import (
    UnavailableTierError,
    WorkflowOutcome,
    available_tiers,
    create_workflow,
    register_workflow,
    supports_tier,
)
from logic.workflows.spark import SPARK_TOOL_NAMES


def test_the_tier_order_is_the_rank():
    assert TIERS == (SPARK, CRAFT, FORGE)
    assert TIER_RANK[SPARK] < TIER_RANK[CRAFT] < TIER_RANK[FORGE]


def test_every_tier_has_a_spec():
    assert set(TIER_SPECS) == set(TIERS)
    assert all(spec.summary and spec.classifier_hint for spec in TIER_SPECS.values())


def test_the_workflow_shapes_match_the_decision():
    assert TIER_SPECS[SPARK].stages == ()
    assert TIER_SPECS[CRAFT].stages == ("plan", "execute", "validate")
    assert not TIER_SPECS[SPARK].is_multi_stage
    assert TIER_SPECS[CRAFT].is_multi_stage


def test_forge_is_a_tier_with_a_design_stage_in_front():
    """DR-054: `forge` ships in both editions; only the decision record is Pro's."""
    assert is_tier("forge")
    assert normalise_tier("forge") == FORGE
    assert supports_tier("forge")
    assert TIER_SPECS[FORGE].stages == ("design", "plan", "execute", "validate")


def test_classification_failure_lands_on_the_middle_tier():
    assert DEFAULT_TIER == CRAFT
    assert normalise_tier("nonsense") == CRAFT
    assert normalise_tier("") == CRAFT
    assert normalise_tier("  CRAFT ") == CRAFT


def test_is_tier_rejects_unknown_names():
    assert is_tier(SPARK) and not is_tier("advanced")


@pytest.mark.parametrize(
    ("current", "proposed", "expected"),
    [
        (None, SPARK, SPARK),
        (SPARK, CRAFT, CRAFT),
        (CRAFT, SPARK, CRAFT),
        (CRAFT, CRAFT, CRAFT),
    ],
)
def test_the_ratchet_only_ever_moves_up(current, proposed, expected):
    assert ratchet(current, proposed) == expected


def test_the_ratchet_tolerates_an_unknown_current_tier():
    assert ratchet("advanced", SPARK) == SPARK


@pytest.mark.parametrize("tier", [SPARK, CRAFT])
def test_every_tier_has_a_registered_workflow(tier):
    assert supports_tier(tier)
    assert tier in available_tiers()
    assert create_workflow(tier).tier == tier


def test_an_unregistered_tier_fails_with_a_useful_message():
    with pytest.raises(UnavailableTierError, match="not implemented yet"):
        create_workflow("nonexistent-tier")


def test_craft_runs_the_stages_its_spec_describes():
    assert create_workflow(CRAFT).stages == TIER_SPECS[CRAFT].stages


def test_registering_a_workflow_for_an_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="not a known tier"):
        register_workflow("advanced", lambda: None)


def test_available_tiers_is_ranked_not_alphabetical():
    ranked = available_tiers()
    assert list(ranked) == sorted(ranked, key=lambda tier: TIER_RANK[tier])


def test_spark_can_read_and_make_a_trivial_edit_but_cannot_create_files():
    """DR-003 keeps spark near-no-write: edit_file yes, write_file no."""
    assert "edit_file" in SPARK_TOOL_NAMES
    assert "write_file" not in SPARK_TOOL_NAMES
    assert {"read_file", "search_files", "find_files"} <= set(SPARK_TOOL_NAMES)


def test_an_outcome_that_stopped_early_is_not_a_success():
    assert WorkflowOutcome(tier=SPARK, answer="x").succeeded
    assert not WorkflowOutcome(tier=SPARK, answer="", stop_reason="round_cap").succeeded
