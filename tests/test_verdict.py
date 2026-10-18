"""DR-005/DR-017: the verdict is derived by the runner, fail-closed."""

from __future__ import annotations

import pytest

from logic.requirements import Requirement
from logic.workflows.verdict import LOOPBACK_TARGETS, derive_verdict


def _reqs(*statuses):
    return [
        Requirement(id=f"R{i}", description=f"req {i}", status=status)
        for i, status in enumerate(statuses, start=1)
    ]


def test_a_pass_with_everything_verified_passes():
    verdict = derive_verdict("VERDICT: PASS", _reqs("Verified", "Verified"))
    assert verdict.passed
    assert verdict.describe() == "passed"


def test_a_claimed_pass_with_an_unverified_requirement_is_overridden():
    """The core DR-005 guarantee."""
    verdict = derive_verdict("VERDICT: PASS", _reqs("Verified", "Failed"))
    assert not verdict.passed
    assert verdict.overridden
    assert verdict.unmet == ("R2",)
    assert "R2" in verdict.feedback
    assert "overridden" in verdict.describe()


def test_an_explicit_failure_is_honoured_with_everything_verified():
    """The ledger is necessary but not sufficient."""
    verdict = derive_verdict(
        "VERDICT: FAIL execute — it breaks existing callers", _reqs("Verified")
    )
    assert not verdict.passed
    assert not verdict.overridden
    assert verdict.target == "execute"
    assert verdict.feedback == "it breaks existing callers"


def test_a_missing_verdict_line_fails_closed():
    """An unreadable gate is not a passed gate."""
    verdict = derive_verdict("Everything looks fine to me.", _reqs("Verified"))
    assert not verdict.passed
    assert "VERDICT: PASS" in verdict.feedback


def test_the_last_verdict_line_wins():
    verdict = derive_verdict(
        "VERDICT: FAIL plan — draft\n\nOn reflection:\n\nVERDICT: PASS", _reqs("Verified")
    )
    assert verdict.passed


@pytest.mark.parametrize("target", LOOPBACK_TARGETS)
def test_each_loopback_target_is_read(target):
    verdict = derive_verdict(f"VERDICT: FAIL {target} — reason", _reqs("Verified"))
    assert verdict.target == target


def test_a_target_the_tier_does_not_have_is_routed_to_one_it_does():
    verdict = derive_verdict(
        "VERDICT: FAIL design — wrong approach",
        _reqs("Verified"),
        available_targets=("plan", "execute"),
    )
    assert verdict.target == "execute"


def test_an_unnamed_target_defaults_to_execute():
    assert derive_verdict("VERDICT: FAIL", _reqs("Verified")).target == "execute"


def test_the_verdict_line_is_case_insensitive():
    assert derive_verdict("verdict: pass", _reqs("Verified")).passed


def test_an_overridden_pass_reports_the_unmet_ids_not_the_stages_feedback():
    verdict = derive_verdict(
        "VERDICT: PASS", _reqs("Failed", "Failed")
    )
    assert set(verdict.unmet) == {"R1", "R2"}
    assert "R1" in verdict.feedback and "R2" in verdict.feedback


def test_an_empty_report_fails_closed():
    assert not derive_verdict("", _reqs("Verified")).passed


def test_only_unverified_gaps_are_flagged_as_unchecked():
    """No defect was observed, so the remedy is checking, not changing code."""
    verdict = derive_verdict("VERDICT: FAIL execute", _reqs("Verified", "Unverified"))
    assert not verdict.passed
    assert verdict.unchecked_only


def test_any_observed_failure_is_not_unchecked_only():
    verdict = derive_verdict(
        "VERDICT: FAIL execute", _reqs("Failed", "Unverified", "Verified")
    )
    assert not verdict.unchecked_only
