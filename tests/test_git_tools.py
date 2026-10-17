"""Read-only git inspection tools (DR-025).

The tests run against a real repository rather than a mocked ``subprocess``:
the properties that matter — a pathspec that cannot become an option, a
non-repository answered rather than raised — are properties of how git itself
parses the argv, and a fake standing in for git would pass against a broken
implementation.
"""

from __future__ import annotations

import subprocess

import pytest

from logic.common.tools.git import (
    GIT_LOG_MAX_COUNT,
    git_diff,
    git_log,
    git_status,
)
from logic.common.workspace import Workspace


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


needs_git = pytest.mark.skipif(not _has_git(), reason="git is not installed")


@pytest.fixture
def repository(tmp_path) -> Workspace:
    """A workspace that is a git repository with one commit."""
    def run(*arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("config", "commit.gpgsign", "false")
    (tmp_path / "tracked.py").write_text("print('one')\n", encoding="utf-8")
    run("add", "tracked.py")
    run("commit", "-q", "-m", "add tracked")
    return Workspace.at(tmp_path)


# -- outside a repository ----------------------------------------------------


@needs_git
def test_a_workspace_without_git_history_is_reported_not_raised(workspace):
    """Plenty of workspaces are not repositories; that is an answer, not a fault."""
    for result in (
        git_status(workspace=workspace),
        git_diff(workspace=workspace),
        git_log(workspace=workspace),
    ):
        assert "Not a git repository" in result


# -- status ------------------------------------------------------------------


@needs_git
def test_status_names_the_branch_and_every_kind_of_change(repository):
    (repository.root / "tracked.py").write_text("print('two')\n", encoding="utf-8")
    (repository.root / "fresh.py").write_text("print('new')\n", encoding="utf-8")

    result = git_status(workspace=repository)

    assert "On branch" in result
    assert "tracked.py" in result and "unstaged modified" in result
    assert "fresh.py" in result and "untracked" in result


@needs_git
def test_status_distinguishes_staged_from_unstaged(repository):
    (repository.root / "tracked.py").write_text("print('two')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.py"], cwd=repository.root, check=True, capture_output=True
    )

    result = git_status(workspace=repository)

    assert "staged modified" in result
    assert "unstaged modified" not in result


@needs_git
def test_a_clean_tree_says_so(repository):
    assert "On branch" in git_status(workspace=repository)


# -- diff --------------------------------------------------------------------


@needs_git
def test_diff_shows_the_working_tree_change(repository):
    (repository.root / "tracked.py").write_text("print('two')\n", encoding="utf-8")

    result = git_diff(workspace=repository)

    assert "-print('one')" in result
    assert "+print('two')" in result


@needs_git
def test_diff_narrows_to_a_path(repository):
    (repository.root / "tracked.py").write_text("print('two')\n", encoding="utf-8")
    (repository.root / "other.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "other.py"], cwd=repository.root, check=True, capture_output=True
    )

    assert "other.py" not in git_diff("tracked.py", workspace=repository)


@needs_git
def test_diff_reports_staged_changes_separately(repository):
    (repository.root / "tracked.py").write_text("print('two')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.py"], cwd=repository.root, check=True, capture_output=True
    )

    assert "No unstaged changes" in git_diff(workspace=repository)
    assert "+print('two')" in git_diff(staged=True, workspace=repository)


@needs_git
def test_a_string_boolean_from_the_model_is_accepted(repository):
    """Small local models routinely send "true" rather than a JSON boolean."""
    (repository.root / "tracked.py").write_text("print('two')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.py"], cwd=repository.root, check=True, capture_output=True
    )

    assert "+print('two')" in git_diff(staged="true", workspace=repository)


@needs_git
def test_an_unchanged_tree_says_so_rather_than_returning_nothing(repository):
    assert "No unstaged changes" in git_diff(workspace=repository)


# -- log ---------------------------------------------------------------------


@needs_git
def test_log_lists_commits_with_their_subject(repository):
    result = git_log(workspace=repository)
    assert "add tracked" in result


@needs_git
def test_log_count_is_clamped_rather_than_refused(repository):
    """A model asking for 10,000 commits gets the cap, not an error."""
    assert "add tracked" in git_log(max_count=10_000, workspace=repository)
    assert "add tracked" in git_log(max_count=0, workspace=repository)


@needs_git
def test_log_rejects_a_non_numeric_count(repository):
    assert "must be an integer" in git_log(max_count="lots", workspace=repository)


@needs_git
def test_log_narrows_to_a_path(repository):
    assert "No commits touching" in git_log(path="never-existed.py", workspace=repository)


# -- the boundary ------------------------------------------------------------


@needs_git
def test_a_path_escaping_the_workspace_is_refused(repository):
    for result in (
        git_diff("../outside.py", workspace=repository),
        git_log(path="../outside.py", workspace=repository),
    ):
        assert "escapes the workspace" in result


@needs_git
@pytest.mark.parametrize(
    "hostile",
    [
        "--output=/tmp/escaped",
        "--upload-pack=touch /tmp/pwned",
        "-x",
        "; touch /tmp/pwned",
        "$(touch /tmp/pwned)",
    ],
)
def test_a_path_that_looks_like_an_option_never_becomes_one(repository, hostile):
    """The `--` separator is what makes these tools ungated (DR-025).

    Each of these reaches git as a pathspec. It either matches nothing or
    escapes the workspace; what it must never do is change git's behaviour.
    """
    result = git_diff(hostile, workspace=repository)

    assert "unknown option" not in result.lower()
    assert "usage:" not in result.lower()
    # Either refused at the workspace boundary, or matched no file at all.
    assert "escapes the workspace" in result or "No unstaged changes" in result


@needs_git
def test_the_handlers_never_reach_a_shell(repository, monkeypatch):
    """A fixed argv with shell=False is the property, so assert on the call."""
    seen: list[dict] = []
    real = subprocess.run

    def spy(arguments, **kwargs):
        seen.append({"arguments": arguments, "kwargs": kwargs})
        return real(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    git_status(workspace=repository)

    assert seen, "git_status did not invoke subprocess"
    for call in seen:
        assert isinstance(call["arguments"], list)
        assert call["arguments"][0] == "git"
        assert not call["kwargs"].get("shell", False)


@needs_git
def test_the_cap_is_what_it_claims_to_be():
    assert GIT_LOG_MAX_COUNT >= 1
