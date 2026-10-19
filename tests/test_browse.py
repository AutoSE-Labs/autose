"""The Files view reads the workspace and nothing else (DR-051)."""

from __future__ import annotations

import os

import pytest

from logic.common.browse import MAX_SHOWN_BYTES, list_workspace, read_workspace_file
from logic.common.workspace import Workspace
from logic.errors import WorkspaceEscapeError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# hi\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "index.js").write_text("x")
    (tmp_path / "secret.txt").write_text("outside")
    return Workspace.at(root)


def test_the_listing_skips_the_folders_the_tools_skip(workspace):
    files, truncated = list_workspace(workspace)
    assert [item["path"] for item in files] == ["README.md", "src/app.py"]
    assert not truncated


def test_a_file_is_read_as_text(workspace):
    shown = read_workspace_file(workspace, "src/app.py")
    assert shown.text == "print('hi')\n"
    assert not shown.binary and not shown.truncated


def test_nothing_outside_the_workspace_is_read(workspace):
    with pytest.raises(WorkspaceEscapeError):
        read_workspace_file(workspace, "../secret.txt")
    os.symlink(workspace.root.parent / "secret.txt", workspace.root / "link.txt")
    with pytest.raises(WorkspaceEscapeError):
        read_workspace_file(workspace, "link.txt")
    assert "link.txt" not in [item["path"] for item in list_workspace(workspace)[0]]


def test_hidden_folders_are_not_read_either(workspace):
    with pytest.raises(PermissionError):
        read_workspace_file(workspace, ".git/config")


def test_binary_and_large_files_are_not_shown_whole(workspace):
    (workspace.root / "blob.bin").write_bytes(b"\0\1\2")
    assert read_workspace_file(workspace, "blob.bin").binary
    (workspace.root / "big.txt").write_text("a" * (MAX_SHOWN_BYTES + 10))
    big = read_workspace_file(workspace, "big.txt")
    assert big.truncated and len(big.text) == MAX_SHOWN_BYTES
