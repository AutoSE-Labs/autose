"""Allowances that outlast one sitting: workspace rules and sealed session
rules (DR-057)."""

from __future__ import annotations

import json
import stat

import pytest

from logic.common.allowances import Allowance
from logic.saved_allowances import (
    ALLOWANCES_FILENAME,
    MAX_WORKSPACE_RULES,
    AllowanceFile,
    recheck_rule,
)
from logic.user_config import user_config_dir

PYTEST = Allowance("run_command", "pytest")
DOCS = Allowance("fetch_url", "docs.python.org")


def _file(tmp_path, workspace=None) -> AllowanceFile:
    return AllowanceFile(tmp_path / "config" / ALLOWANCES_FILENAME, workspace or tmp_path / "work")


# -- re-checking what comes back -------------------------------------------------


def test_a_rule_today_s_rules_allow_comes_back_as_it_was():
    assert recheck_rule("run_command", "python -m pytest") == Allowance("run_command", "python -m pytest")
    assert recheck_rule("fetch_url", "docs.python.org") == DOCS
    assert recheck_rule("web_search", "") == Allowance("web_search")


@pytest.mark.parametrize(
    ("tool", "pattern"),
    [
        ("run_command", "rm"),  # can only ever be allowed once
        ("run_command", "pytest -q"),  # not the leading words
        ("run_command", "sudo pytest"),  # a wrapper
        ("fetch_url", "https://docs.python.org/3/"),  # tidied, so not written by AutoSE
        ("web_search", "anything"),  # a whole-tool rule has no pattern
        ("delete_everything", ""),  # not a tool a rule may name
    ],
)
def test_a_rule_today_s_rules_refuse_is_not_trusted(tool, pattern):
    with pytest.raises(ValueError):
        recheck_rule(tool, pattern)


# -- workspace rules ---------------------------------------------------------------


def test_the_default_file_is_in_the_user_config_directory_not_the_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    rules = AllowanceFile.for_workspace(workspace)
    rules.add(PYTEST)

    assert rules.path == user_config_dir() / ALLOWANCES_FILENAME
    assert rules.path.is_file()
    assert not rules.path.resolve().is_relative_to(workspace.resolve())
    assert list(workspace.iterdir()) == []


def test_the_file_is_private(tmp_path):
    rules = _file(tmp_path)
    rules.add(PYTEST)
    assert stat.S_IMODE(rules.path.stat().st_mode) == 0o600


def test_a_workspace_rule_is_kept_for_that_workspace_only(tmp_path):
    here = _file(tmp_path, tmp_path / "here")
    there = _file(tmp_path, tmp_path / "there")
    here.add(PYTEST)

    assert _file(tmp_path, tmp_path / "here").rules() == (PYTEST,)
    assert there.rules() == ()


def test_workspaces_are_keyed_by_their_resolved_path(tmp_path):
    (tmp_path / "work").mkdir()
    _file(tmp_path, tmp_path / "work").add(PYTEST)
    assert _file(tmp_path, tmp_path / "work" / ".." / "work").rules() == (PYTEST,)


def test_adding_the_same_rule_twice_keeps_one(tmp_path):
    rules = _file(tmp_path)
    rules.add(PYTEST)
    assert rules.add(PYTEST) == (PYTEST,)


def test_a_refused_rule_never_reaches_the_file(tmp_path):
    rules = _file(tmp_path)
    with pytest.raises(ValueError):
        rules.add(Allowance("run_command", "rm"))
    assert not rules.path.exists()


def test_a_rule_can_be_taken_back(tmp_path):
    rules = _file(tmp_path)
    rules.add(PYTEST)
    rules.add(DOCS)

    assert rules.remove(PYTEST.key) is True
    assert rules.rules() == (DOCS,)
    assert rules.remove(PYTEST.key) is False


def test_a_hand_edited_rule_today_s_rules_refuse_is_dropped_and_reported(tmp_path):
    rules = _file(tmp_path)
    rules.add(PYTEST)
    document = json.loads(rules.path.read_text(encoding="utf-8"))
    document["workspaces"][str((tmp_path / "work").resolve())].append(
        {"tool": "run_command", "pattern": "rm"}
    )
    rules.path.write_text(json.dumps(document), encoding="utf-8")

    assert rules.rules() == (PYTEST,)
    assert any("rm" in problem for problem in rules.problems)


def test_an_unreadable_file_reads_as_no_rules(tmp_path):
    rules = _file(tmp_path)
    rules.path.parent.mkdir(parents=True)
    rules.path.write_text("{not json", encoding="utf-8")
    assert rules.rules() == ()
    # And the next write replaces it with a good one.
    assert rules.add(PYTEST) == (PYTEST,)


def test_a_workspace_keeps_a_bounded_number_of_rules(tmp_path):
    rules = _file(tmp_path)
    for index in range(MAX_WORKSPACE_RULES + 3):
        rules.add(Allowance("run_command", f"tool{index}"))
    kept = rules.rules()
    assert len(kept) == MAX_WORKSPACE_RULES
    assert kept[-1].pattern == f"tool{MAX_WORKSPACE_RULES + 2}"


# -- a session's own rules, sealed -------------------------------------------------


def test_a_sealed_session_s_rules_come_back(tmp_path):
    rules = _file(tmp_path)
    seal = rules.seal("s1", (PYTEST, DOCS))

    kept, dropped = rules.restore("s1", [("run_command", "pytest"), ("fetch_url", "docs.python.org")], seal)

    assert kept == (PYTEST, DOCS)
    assert dropped == ()


def test_rules_with_no_seal_are_not_restored(tmp_path):
    """A session file that came in a clone carries rules nobody here gave."""
    rules = _file(tmp_path)
    rules.seal("other", (PYTEST,))  # this machine has a key

    kept, dropped = rules.restore("s1", [("run_command", "python evil.py")], "")

    assert kept == ()
    assert "not restored" in dropped[0]


@pytest.mark.parametrize(
    "change",
    [
        lambda pairs, sid, ws: ([*pairs, ("run_command", "python evil.py")], sid, ws),
        lambda pairs, sid, ws: (pairs, "s2", ws),
        lambda pairs, sid, ws: (pairs, sid, "elsewhere"),
    ],
    ids=["a rule added by hand", "another session", "another workspace"],
)
def test_a_seal_opens_only_for_the_rules_session_and_workspace_it_was_made_for(tmp_path, change):
    rules = _file(tmp_path)
    seal = rules.seal("s1", (PYTEST,))
    pairs, session_id, workspace = change([("run_command", "pytest")], "s1", "work")

    kept, _ = _file(tmp_path, tmp_path / workspace).restore(session_id, pairs, seal)

    assert kept == ()


def test_a_seal_made_with_another_machine_s_key_does_not_open(tmp_path):
    seal = _file(tmp_path / "theirs", tmp_path / "work").seal("s1", (PYTEST,))
    mine = _file(tmp_path / "mine", tmp_path / "work")
    mine.seal("other", (DOCS,))

    assert mine.restore("s1", [("run_command", "pytest")], seal)[0] == ()


def test_a_sealed_rule_today_s_rules_refuse_is_dropped_with_a_reason(tmp_path):
    """Sealed when the rules were looser; checked again when it comes back."""
    rules = _file(tmp_path)
    rm = Allowance("run_command", "rm")
    seal = rules.seal("s1", (PYTEST, rm))

    kept, dropped = rules.restore("s1", [("run_command", "pytest"), ("run_command", "rm")], seal)

    assert kept == (PYTEST,)
    assert dropped == ("rm … commands: rm can only be allowed one call at a time",)


def test_nothing_is_written_to_seal_no_rules(tmp_path):
    rules = _file(tmp_path)
    assert rules.seal("s1", ()) == ""
    assert not rules.path.exists()


def test_losing_the_file_means_saved_rules_ask_again(tmp_path):
    rules = _file(tmp_path)
    seal = rules.seal("s1", (PYTEST,))
    rules.path.unlink()

    assert rules.restore("s1", [("run_command", "pytest")], seal)[0] == ()
