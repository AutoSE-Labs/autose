"""Path-boundary enforcement — the check every filesystem tool relies on."""

from __future__ import annotations

import pytest

from logic.errors import WorkspaceEscapeError


def test_relative_path_resolves_under_root(workspace):
    assert workspace.resolve("pkg/module.py") == workspace.root / "pkg" / "module.py"


def test_absolute_path_inside_root_is_allowed(workspace):
    inside = workspace.root / "notes.md"
    assert workspace.resolve(str(inside)) == inside


def test_parent_traversal_is_refused(workspace):
    with pytest.raises(WorkspaceEscapeError):
        workspace.resolve("../outside.txt")


def test_absolute_path_outside_root_is_refused(workspace):
    with pytest.raises(WorkspaceEscapeError):
        workspace.resolve("/etc/passwd")


def test_symlink_pointing_outside_is_refused_by_target(workspace, tmp_path):
    outside = tmp_path.parent / "escape-target"
    outside.mkdir(exist_ok=True)
    try:
        (workspace.root / "link").symlink_to(outside)
    except OSError as exc:  # Windows without Developer Mode or admin
        pytest.skip(f"cannot create symlinks here: {exc}")
    with pytest.raises(WorkspaceEscapeError):
        workspace.resolve("link/secret.txt")


def test_display_is_relative_and_posix(workspace):
    assert workspace.display(workspace.root / "a" / "b.py") == "a/b.py"


def test_contains_reports_without_raising(workspace):
    assert workspace.contains("src/thing.py")
    assert not workspace.contains("../thing.py")
