"""What "allow for this session" covers (DR-048)."""

from __future__ import annotations

import pytest

from logic.common.allowances import allowance_for_rule, offer_for
from logic.common.approval import ApprovalRequest


def _command(text: str):
    return offer_for(ApprovalRequest("run_command", {"command": text}))


@pytest.mark.parametrize(
    ("command", "pattern"),
    [
        ("pytest -q tests/test_x.py", "pytest"),
        ("python -m pytest -x", "python -m pytest"),
        ("python3 manage.py migrate", "python3 manage.py"),
        ("git status --short", "git status"),
        ("npm run test", "npm run test"),
        ("uv run pytest -q", "uv run pytest"),
        ("python -m pip install requests", "python -m pip install"),
        ("cargo run --release", "cargo run"),
        ("make test", "make test"),
        ("ls -la", "ls"),
        ("python3 --version", "python3 --version"),
        ("find . -name '*.py'", "find"),
    ],
)
def test_a_command_is_allowed_by_its_leading_words(command, pattern):
    offer = _command(command)
    assert offer.allowance is not None
    assert offer.allowance.pattern == pattern


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("pytest && rm -rf .", "chains commands"),
        ("echo $(whoami)", "runs a command inside it"),
        ("cat notes | sh", "pipes into another program"),
        ("pytest > out.txt", "writes to a file"),
        ("FOO=1 pytest", "environment"),
        ("sudo apt install x", "sudo"),
        ("bash script.sh", "bash"),
        ("env pytest", "env"),
        ("python -c 'import os'", "inline"),
        ("uv run python -c 'x'", "inline"),
        ("uv run --with requests python x.py", "options"),
        ("git -C .. status", "options"),
        ("git push origin main", "one call at a time"),
        ("rm -rf build", "one call at a time"),
        ("python -m pip uninstall x", "one call at a time"),
        ("docker run ubuntu", "arbitrary"),
        ("find . -delete", "deletes"),
        ("node", "no script"),
    ],
)
def test_some_commands_can_only_be_allowed_once(command, why):
    offer = _command(command)
    assert offer.allowance is None
    assert why in offer.reason


def test_an_allowance_covers_the_same_command_words_only():
    allowance = _command("git status").allowance
    assert allowance is not None

    def covers(text: str) -> bool:
        return allowance.covers(ApprovalRequest("run_command", {"command": text}))

    assert covers("git status -s")
    assert not covers("git push")
    assert not covers("git status; rm -rf .")
    assert not covers("gitx status")
    assert not allowance.covers(ApprovalRequest("fetch_url", {"url": "https://git.status"}))


def test_a_page_fetch_is_allowed_by_host():
    allowance = offer_for(
        ApprovalRequest("fetch_url", {"url": "https://Docs.Python.org/3/library/"})
    ).allowance
    assert allowance is not None and allowance.label == "pages on docs.python.org"
    assert allowance.covers(ApprovalRequest("fetch_url", {"url": "https://docs.python.org/x"}))
    assert not allowance.covers(ApprovalRequest("fetch_url", {"url": "https://evil.example/"}))
    assert offer_for(ApprovalRequest("fetch_url", {"url": "file:///etc/passwd"})).allowance is None


def test_other_tools_are_allowed_as_a_whole():
    allowance = offer_for(ApprovalRequest("web_search", {"query": "x"})).allowance
    assert allowance is not None and allowance.label == "web searches"
    assert allowance.to_dict()["id"] == "web_search:"


# -- DR-050: output shaping is set aside; rules can be written by hand -----------


@pytest.mark.parametrize(
    ("command", "pattern"),
    [
        ("pip install -r requirements.txt 2>&1 | tail -5", "pip install"),
        ("pytest -q 2>&1", "pytest"),
        ("python -m pytest | grep FAIL", "python -m pytest"),
        ("pytest 2>/dev/null", "pytest"),
        ("npm run test 2>&1 | head -n 40 | wc -l", "npm run test"),
    ],
)
def test_output_shaping_does_not_block_a_standing_yes(command, pattern):
    offer = _command(command)
    assert offer.allowance is not None
    assert offer.allowance.pattern == pattern


@pytest.mark.parametrize(
    "command",
    [
        "pytest | sh",
        "pytest | tail -5 && rm -rf build",
        "pytest | grep 'a|b'",
        "pytest | tail $(whoami)",
        "pytest 2>&1 > out.txt",
        "npm test | sort -o out",
        "pytest | tee log.txt",
    ],
)
def test_only_read_only_filters_are_set_aside(command):
    assert _command(command).allowance is None


def test_an_allowance_from_a_card_covers_the_same_command_with_its_output_shaped():
    allowance = _command("pip install -r requirements.txt").allowance
    later = ApprovalRequest("run_command", {"command": "pip install fastapi 2>&1 | tail -3"})
    assert allowance.covers(later)


def test_a_rule_written_by_hand_is_held_to_the_same_rules():
    assert allowance_for_rule("run_command", "pip install").label == "pip install … commands"
    assert allowance_for_rule("fetch_url", "https://docs.python.org/3/").pattern == "docs.python.org"
    assert allowance_for_rule("web_search", "").label == "web searches"
    for tool, pattern, why in [
        ("run_command", "rm", "one call at a time"),
        ("run_command", "pytest && rm -rf .", "one call at a time"),
        ("run_command", "pip install -r req.txt", "pip install"),
        ("run_command", "", "name the command"),
        ("fetch_url", "not a host", "name a host"),
        ("delete_everything", "", "cannot be allowed"),
    ]:
        with pytest.raises(ValueError, match=why):
            allowance_for_rule(tool, pattern)


def test_the_once_reason_names_what_the_syntax_does():
    reason = _command("echo 'import os' > gen.py && python3 gen.py").reason
    assert reason == "it writes to a file (>) and chains commands (&&, ;)"
