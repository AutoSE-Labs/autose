"""Read-only filesystem tools, whole-file write, and shell confinement."""

from __future__ import annotations

from logic.common.tools.filesystem import (
    READ_FILE_DEFAULT_MAX_LINES,
    find_files,
    list_files,
    read_file,
    search_files,
    write_file,
)
from logic.common.tools.shell import run_command


def test_read_file_reports_range_and_total(workspace):
    (workspace.root / "m.py").write_text("a\nb\nc\n", encoding="utf-8")
    result = read_file("m.py", workspace=workspace)
    assert "m.py  (lines 1-3 of 3)" in result
    assert result.endswith("a\nb\nc")


def test_read_file_truncates_long_files_and_says_how_to_continue(workspace):
    lines = "\n".join(str(number) for number in range(500))
    (workspace.root / "big.txt").write_text(lines, encoding="utf-8")
    result = read_file("big.txt", workspace=workspace)
    assert "Truncated" in result
    assert "Continue with read_file(path='big.txt', start_line=" in result
    assert result.count("\n") <= READ_FILE_DEFAULT_MAX_LINES + 4


def test_read_file_honours_an_explicit_range(workspace):
    (workspace.root / "m.txt").write_text("1\n2\n3\n4\n", encoding="utf-8")
    result = read_file("m.txt", start_line=2, end_line=3, workspace=workspace)
    assert "2\n3" in result and "\n4" not in result


def test_read_file_errors_are_returned_not_raised(workspace):
    assert "file not found" in read_file("absent.txt", workspace=workspace)
    assert "escapes the workspace" in read_file("../x", workspace=workspace)


def test_list_files_and_find_files_skip_noise_directories(workspace):
    (workspace.root / "pkg").mkdir()
    (workspace.root / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    (workspace.root / "__pycache__").mkdir()
    (workspace.root / "__pycache__" / "mod.py").write_text("x", encoding="utf-8")

    listed = list_files(".", "*.py", recursive=True, workspace=workspace)
    found = find_files("*.py", workspace=workspace)
    assert "pkg/mod.py" in listed and "__pycache__" not in listed
    assert "pkg/mod.py" in found and "__pycache__" not in found


def test_search_files_reports_path_and_line_number(workspace):
    (workspace.root / "m.py").write_text("import os\nvalue = 1\n", encoding="utf-8")
    result = search_files("value", workspace=workspace)
    assert "m.py:2: value = 1" in result


def test_search_files_searches_a_file_named_as_the_directory(workspace):
    (workspace.root / "m.py").write_text("import os\nvalue = 1\n", encoding="utf-8")
    result = search_files("value", directory="m.py", workspace=workspace)
    assert "m.py:2: value = 1" in result


def test_search_files_caps_results(workspace):
    (workspace.root / "m.txt").write_text("hit\n" * 100, encoding="utf-8")
    result = search_files("hit", max_results=5, workspace=workspace)
    assert "results capped at 5" in result


def test_search_files_reports_an_invalid_regex(workspace):
    assert "invalid pattern" in search_files("(unclosed", workspace=workspace)


def test_write_file_creates_parent_directories(workspace):
    result = write_file("deep/nested/m.py", "print(1)\n", workspace=workspace)
    assert (workspace.root / "deep" / "nested" / "m.py").read_text() == "print(1)\n"
    assert "deep/nested/m.py" in result


def test_write_file_refuses_to_escape_the_workspace(workspace):
    assert "escapes the workspace" in write_file("../x.py", "", workspace=workspace)


def test_run_command_returns_output_and_exit_code(workspace):
    result = run_command("echo hello", workspace=workspace)
    assert "hello" in result and "Exit code: 0" in result


def test_run_command_reports_a_failing_exit_code(workspace):
    assert "Exit code: 3" in run_command("exit 3", workspace=workspace)


def test_run_command_refuses_absolute_paths_parents_and_cd(workspace):
    assert "absolute path" in run_command("cat /etc/passwd", workspace=workspace)
    assert "parent path" in run_command("cat ../secret", workspace=workspace)
    assert "may not change directories" in run_command("cd sub && ls", workspace=workspace)


def test_run_command_refuses_root_globs_and_double_slashes(workspace):
    assert "absolute path" in run_command("rm -rf /*", workspace=workspace)
    assert "absolute path" in run_command("rm -rf //", workspace=workspace)


def test_run_command_refuses_home_directory_references(workspace):
    assert "home directory" in run_command("cat ~/.ssh/id_rsa", workspace=workspace)
    assert "home directory" in run_command("cat $HOME/.ssh/id_rsa", workspace=workspace)
    assert "home directory" in run_command("cat ${HOME}/.ssh/id_rsa", workspace=workspace)


def test_run_command_confines_cwd_to_the_workspace(workspace):
    assert "escapes the workspace" in run_command("ls", cwd="..", workspace=workspace)


# -- build caches are not part of the project -----------------------------------
#
# A repo that has run mypy, pytest and ruff carries hundreds of hash-named
# cache files. Listed, they were the first thing a model saw of the project and
# they filled the window before anything that says what the project *is*.


def _with_caches(workspace):
    for cache, name in (
        (".mypy_cache", "cache.0.db"),
        (".pytest_cache", "nodeids"),
        (".ruff_cache", "1234567890"),
        ("__pycache__", "m.cpython-312.pyc"),
    ):
        (workspace.root / cache).mkdir()
        (workspace.root / cache / name).write_text("x", encoding="utf-8")
    (workspace.root / "src").mkdir()
    (workspace.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (workspace.root / "README.md").write_text("# Project\n", encoding="utf-8")


def test_a_recursive_listing_leaves_out_build_caches(workspace):
    _with_caches(workspace)
    result = list_files(".", recursive=True, workspace=workspace)
    assert "src/app.py" in result and "README.md" in result
    for cache in (".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"):
        assert cache not in result


def test_a_flat_listing_leaves_them_out_too(workspace):
    """The non-recursive branch filtered nothing at all, which is where a
    model's *first* look at a repository usually lands."""
    _with_caches(workspace)
    result = list_files(".", workspace=workspace)
    assert "src" in result and "README.md" in result
    for cache in (".mypy_cache", ".pytest_cache", ".ruff_cache"):
        assert cache not in result


# -- one truncation notice, not two ---------------------------------------------


def test_read_file_is_bounded_by_characters_as_well_as_lines(workspace):
    """150 lines of prose is far more text than 150 lines of code.

    Unbounded on this axis, an oversized result was truncated a second time by
    `BoundedToolOutput`, and the model was handed two truncation notices naming
    two different recovery routes for one call.
    """
    (workspace.root / "wide.txt").write_text(
        "\n".join("w" * 400 for _ in range(150)), encoding="utf-8"
    )
    result = read_file("wide.txt", workspace=workspace, max_chars=2000)
    assert len(result) < 2400  # the body, plus header and notice
    assert result.count("[Truncated") == 1
    assert "start_line=" in result


def test_a_single_line_longer_than_the_whole_budget_still_returns_its_start(workspace):
    (workspace.root / "min.js").write_text("z" * 50_000, encoding="utf-8")
    result = read_file("min.js", workspace=workspace, max_chars=1000)
    assert "z" * 500 in result
    assert len(result) < 1400


def test_an_explicit_range_that_was_honoured_in_full_is_not_called_truncated(workspace):
    (workspace.root / "m.txt").write_text(
        "\n".join(str(n) for n in range(500)), encoding="utf-8"
    )
    result = read_file("m.txt", start_line=2, end_line=4, workspace=workspace)
    assert "Truncated" not in result


def test_write_file_append_adds_to_the_end(workspace):
    write_file("big.py", "a = 1\n", workspace=workspace)
    result = write_file("big.py", "b = 2\n", append=True, workspace=workspace)
    assert (workspace.root / "big.py").read_text(encoding="utf-8") == "a = 1\nb = 2\n"
    assert "2 in total" in result
