"""DR-011: oversized tool output is bounded but recoverable."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from logic.common.tool_output import (
    TOOL_OUTPUT_DIRECTORY,
    BoundedToolOutput,
    FileToolOutputStore,
    NullToolOutputStore,
)


def _bounded(workspace, max_chars: int = 400) -> BoundedToolOutput:
    return BoundedToolOutput(FileToolOutputStore(workspace), max_chars=max_chars)


def test_output_within_the_cap_is_untouched(workspace):
    content, artifact = _bounded(workspace).apply("run_command", {}, "short output")
    assert content == "short output"
    assert artifact is None


def test_oversized_output_keeps_both_the_beginning_and_the_end(workspace):
    result = "HEAD" + ("x" * 5000) + "TAIL-THE-ACTUAL-FAILURE"
    content, artifact = _bounded(workspace).apply("run_command", {}, result)
    assert content.startswith("HEAD")
    assert content.endswith("TAIL-THE-ACTUAL-FAILURE")
    assert "Output truncated" in content
    assert artifact is not None


def test_preview_respects_the_character_cap(workspace):
    content, _ = _bounded(workspace, max_chars=500).apply("t", {}, "y" * 20_000)
    assert len(content) <= 500 + 4  # the two blank-line separators


def test_full_output_is_recoverable_from_the_reported_path(workspace):
    result = "z" * 9000
    content, artifact = _bounded(workspace).apply("run_command", {"command": "x"}, result)
    assert artifact in content
    assert (workspace.root / artifact).read_text(encoding="utf-8") == result


def test_artifacts_live_in_the_git_ignored_autose_directory(workspace):
    _, artifact = _bounded(workspace).apply("run_command", {}, "q" * 9000)
    assert artifact.startswith(TOOL_OUTPUT_DIRECTORY)
    assert (workspace.root / ".autose" / ".gitignore").read_text() == "*\n"


def test_identical_calls_deduplicate_to_one_artifact(workspace):
    bounded = _bounded(workspace)
    first = bounded.apply("run_command", {"command": "make"}, "d" * 9000)[1]
    second = bounded.apply("run_command", {"command": "make"}, "d" * 9000)[1]
    assert first == second
    directory = workspace.root / TOOL_OUTPUT_DIRECTORY
    assert len(list(directory.glob("*.txt"))) == 1


def test_different_arguments_yield_different_artifacts(workspace):
    bounded = _bounded(workspace)
    first = bounded.apply("run_command", {"command": "a"}, "d" * 9000)[1]
    second = bounded.apply("run_command", {"command": "b"}, "d" * 9000)[1]
    assert first != second


def test_retention_removes_the_oldest_artifacts(workspace):
    bounded = BoundedToolOutput(
        FileToolOutputStore(workspace, max_artifacts=3), max_chars=200
    )
    for index in range(8):
        bounded.apply("run_command", {"n": index}, f"{index}" * 5000)
    directory = workspace.root / TOOL_OUTPUT_DIRECTORY
    assert len(list(directory.glob("*.txt"))) <= 3


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permission bits do not apply on Windows"
)
def test_artifacts_are_owner_readable_only(workspace):
    _, artifact = _bounded(workspace).apply("run_command", {}, "s" * 9000)
    mode = stat.S_IMODE(os.stat(workspace.root / artifact).st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_a_failed_save_still_returns_a_preview_with_a_notice(workspace):
    bounded = BoundedToolOutput(NullToolOutputStore(), max_chars=300)
    content, artifact = bounded.apply("run_command", {}, "HEAD" + "x" * 9000 + "TAIL")
    assert artifact is None
    assert "could not be saved" in content
    assert content.startswith("HEAD") and content.endswith("TAIL")


def test_tool_name_is_sanitised_into_the_artifact_filename(workspace):
    _, artifact = _bounded(workspace).apply("weird/../name", {}, "p" * 9000)
    assert "/" not in artifact.rsplit("/", 1)[-1].replace(".txt", "")
    assert (workspace.root / artifact).is_file()
