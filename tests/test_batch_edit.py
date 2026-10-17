"""DR-007: the validated, batch single-file edit tool."""

from __future__ import annotations

import codecs

from logic.common.tools.editing import MAX_EDITS_PER_CALL, edit_file


def _write(workspace, name: str, content: str):
    path = workspace.root / name
    path.write_text(content, encoding="utf-8")
    return path


def test_applies_every_replacement_in_one_call(workspace):
    path = _write(workspace, "m.py", "alpha\nbeta\ngamma\n")
    result = edit_file(
        "m.py",
        [
            {"old_str": "alpha", "new_str": "ALPHA"},
            {"old_str": "gamma", "new_str": "GAMMA"},
        ],
        workspace=workspace,
    )
    assert "Edited 2 block(s)" in result
    assert path.read_text() == "ALPHA\nbeta\nGAMMA\n"


def test_result_is_independent_of_the_order_edits_are_listed_in(workspace):
    forward = _write(workspace, "a.txt", "one two three")
    edit_file(
        "a.txt",
        [{"old_str": "one", "new_str": "1"}, {"old_str": "three", "new_str": "3"}],
        workspace=workspace,
    )
    reverse = _write(workspace, "b.txt", "one two three")
    edit_file(
        "b.txt",
        [{"old_str": "three", "new_str": "3"}, {"old_str": "one", "new_str": "1"}],
        workspace=workspace,
    )
    assert forward.read_text() == reverse.read_text() == "1 two 3"


def test_matching_is_against_the_original_not_intermediate_results(workspace):
    # Rewriting "a" -> "b" first would make the second edit ambiguous if
    # matching used intermediate state; against the original both are unique.
    path = _write(workspace, "m.txt", "a\nb\n")
    result = edit_file(
        "m.txt",
        [{"old_str": "a", "new_str": "b"}, {"old_str": "b", "new_str": "c"}],
        workspace=workspace,
    )
    assert "Edited 2 block(s)" in result
    assert path.read_text() == "b\nc\n"


def test_ambiguous_match_rejects_the_whole_batch(workspace):
    path = _write(workspace, "m.txt", "dup\ndup\nkeep\n")
    result = edit_file(
        "m.txt",
        [
            {"old_str": "keep", "new_str": "KEEP"},
            {"old_str": "dup", "new_str": "x"},
        ],
        workspace=workspace,
    )
    assert "appears 2 times" in result
    assert path.read_text() == "dup\ndup\nkeep\n"


def test_missing_match_rejects_the_whole_batch(workspace):
    path = _write(workspace, "m.txt", "hello\n")
    result = edit_file(
        "m.txt",
        [
            {"old_str": "hello", "new_str": "hi"},
            {"old_str": "absent", "new_str": "x"},
        ],
        workspace=workspace,
    )
    assert "was not found" in result
    assert path.read_text() == "hello\n"


def test_empty_old_str_is_refused(workspace):
    _write(workspace, "m.txt", "content")
    assert "cannot be empty" in edit_file(
        "m.txt", [{"old_str": "", "new_str": "x"}], workspace=workspace
    )


def test_no_op_replacement_is_refused(workspace):
    _write(workspace, "m.txt", "same")
    assert "would make no change" in edit_file(
        "m.txt", [{"old_str": "same", "new_str": "same"}], workspace=workspace
    )


def test_overlapping_ranges_are_refused(workspace):
    path = _write(workspace, "m.txt", "abcdef")
    result = edit_file(
        "m.txt",
        [{"old_str": "abcd", "new_str": "X"}, {"old_str": "cdef", "new_str": "Y"}],
        workspace=workspace,
    )
    assert "overlapping" in result
    assert path.read_text() == "abcdef"


def test_batch_size_is_capped(workspace):
    _write(workspace, "m.txt", "x" * 40)
    edits = [{"old_str": "x", "new_str": "y"}] * (MAX_EDITS_PER_CALL + 1)
    assert "at most" in edit_file("m.txt", edits, workspace=workspace)


def test_empty_batch_is_refused(workspace):
    _write(workspace, "m.txt", "x")
    assert "at least one" in edit_file("m.txt", [], workspace=workspace)


def test_missing_edits_argument_is_refused(workspace):
    _write(workspace, "m.txt", "x")
    assert "'edits' is required" in edit_file("m.txt", workspace=workspace)


def test_utf8_bom_is_preserved(workspace):
    path = workspace.root / "bom.txt"
    path.write_bytes(codecs.BOM_UTF8 + b"alpha\n")
    edit_file("bom.txt", [{"old_str": "alpha", "new_str": "beta"}], workspace=workspace)
    raw = path.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    assert raw[len(codecs.BOM_UTF8) :].decode("utf-8") == "beta\n"


def test_consistent_crlf_endings_survive(workspace):
    path = workspace.root / "crlf.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    edit_file("crlf.txt", [{"old_str": "two", "new_str": "2"}], workspace=workspace)
    assert path.read_bytes() == b"one\r\n2\r\n"


def test_crlf_old_str_matches_a_crlf_file(workspace):
    path = workspace.root / "crlf.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    result = edit_file(
        "crlf.txt", [{"old_str": "one\r\ntwo", "new_str": "1\r\n2"}], workspace=workspace
    )
    assert "Edited 1 block(s)" in result
    assert path.read_bytes() == b"1\r\n2\r\n"


def test_mixed_endings_get_plain_exact_matching(workspace):
    path = workspace.root / "mixed.txt"
    path.write_bytes(b"one\r\ntwo\nthree\r\n")
    edit_file("mixed.txt", [{"old_str": "two", "new_str": "2"}], workspace=workspace)
    assert path.read_bytes() == b"one\r\n2\nthree\r\n"


def test_path_escape_is_refused(workspace):
    assert "escapes the workspace" in edit_file(
        "../outside.txt", [{"old_str": "a", "new_str": "b"}], workspace=workspace
    )


def test_missing_file_is_reported_not_created(workspace):
    assert "file not found" in edit_file(
        "absent.txt", [{"old_str": "a", "new_str": "b"}], workspace=workspace
    )
    assert not (workspace.root / "absent.txt").exists()


def test_non_utf8_file_is_refused(workspace):
    path = workspace.root / "bin.dat"
    path.write_bytes(b"\xff\xfe\x00binary")
    assert "not valid UTF-8" in edit_file(
        "bin.dat", [{"old_str": "a", "new_str": "b"}], workspace=workspace
    )


def test_malformed_edit_entries_are_refused(workspace):
    _write(workspace, "m.txt", "x")
    assert "must be an object" in edit_file(
        "m.txt", ["not-an-object"], workspace=workspace
    )
    assert "requires string" in edit_file(
        "m.txt", [{"old_str": "x", "new_str": 5}], workspace=workspace
    )
