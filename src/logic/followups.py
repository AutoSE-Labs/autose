"""What to ask next, suggested from how a run ended (DR-063).

After a run, the next request is often already decided by how it ended: a
check that did not pass, a stop that left the work half done, a changed file
with no test beside it. These rules name that request from the finished turn
alone. No model is asked: a suggestion that costs a generation would hold the
GPU the person is about to need, arrive seconds after they had started typing,
and could differ between the dashboard and the terminal. A pure function of
the turn is instant, free, and the same in both.

Deliberately narrow. A suggestion is made only when something specific can be
named — a requirement, a file, a function — because "tell me more" fits every
answer, and a row of vague suggestions teaches a person to ignore the row.
Fewer good suggestions beat three filled slots.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the session imports half of logic; this needs only its shape
    from .session import Turn

__all__ = [
    "MAX_FOLLOW_UPS",
    "FollowUp",
    "suggest_follow_ups",
]

#: The most suggestions a run gets. Three is what fits above a request box at
#: a glance; past that a person is reading a list, not choosing.
MAX_FOLLOW_UPS = 3

#: The most requirement suggestions one run gets, so a run with many unmet
#: checks still leaves room for a different kind of next step.
_MAX_REQUIREMENTS = 2

#: How long a label may be before it is shortened at a word. The prompt behind
#: it is never shortened: it is what the model reads.
_LABEL_CHARS = 56

_STOPPED_BY_PERSON = frozenset({"cancelled"})
#: DR-009/DR-030: a bound that ended the run. A new run starts with a fresh
#: budget, so carrying on is a real option, unlike retrying a fault.
_STOPPED_BY_LIMIT = frozenset({"round_cap", "attempts_exhausted", "time_budget_exhausted"})

#: What went wrong, in words a person would use, for the "Try again" label.
_WHAT_FAILED = {
    "empty_answer": "the model's reply was empty",
    "output_budget_exhausted": "the model ran out of room to answer",
    "stalled": "it went in circles",
    "context_thrashing": "the context window was too small",
    "no_result": "nothing was produced",
}

#: A decision-record number is for contributors, never for a person using the
#: app, and a requirement or a file name can carry one in from the model.
_RECORD_NUMBER = re.compile(r"\bDR-\d+")

_TEST_DIRECTORIES = frozenset({"test", "tests", "testing", "spec", "specs", "__tests__"})

#: Source files a test could be written for. Docs, config and data are left
#: out: "add tests for README.md" is not a suggestion anyone wants.
_CODE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go",
        ".rs", ".java", ".kt", ".scala", ".c", ".h", ".cc", ".cpp", ".hpp",
        ".cs", ".rb", ".php", ".swift", ".dart", ".lua", ".ex", ".exs", ".sh",
    }
)

#: A workspace-relative path to a source file, as an answer mentions one. An
#: absolute path or a URL never matches: neither can start after a "/". Only
#: a mention in code formatting or with a folder in it counts (see
#: :func:`_mentioned_paths`), so "Node.js" in prose is not taken for a file.
_MENTIONED_PATH = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:"
    + "|".join(sorted(suffix[1:] for suffix in _CODE_SUFFIXES))
    + r"))(?![\w/-])"
)
#: A function named in code formatting, as in "`ratchet()`".
_MENTIONED_FUNCTION = re.compile(r"`([A-Za-z_][\w.]{2,})\(\)`")

#: Ways an answer offers a change instead of making one. A heuristic, kept to
#: phrasings that are hard to use for anything else, and only trusted in an
#: answer that names some code (see :func:`_proposes_a_change`).
_PROPOSES_A_CHANGE = re.compile(
    "|".join(
        (
            r"\b(?:would you like|do you want|shall i|should i|want me to)\b[^.?\n]{0,80}?"
            r"\b(?:change|fix|apply|update|add|make|implement|edit|rename|remove|refactor|write)\b",
            r"\bi(?: would|['’]d)? (?:suggest|recommend|propose)\b",
            r"\byou (?:could|should|can|might want to|may want to) "
            r"(?:change|replace|add|update|rename|remove|move|wrap|guard|fix|edit|refactor|switch)\b",
            r"\bto fix (?:this|it|that)\b",
            r"\b(?:change|replace) `[^`\n]+` (?:to|with)\b",
            r"\bhere(?:'s| is) (?:a|the) (?:fix|patch|change)\b",
        )
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FollowUp:
    """One suggested next request."""

    #: What the chip or the hint line shows: short, specific, plain words.
    label: str
    #: What goes into the request box. Complete on its own, because a run that
    #: did not finish never reaches the model's history (DR-004): "carry on"
    #: alone would mean nothing to it.
    prompt: str


def suggest_follow_ups(turn: Turn) -> tuple[FollowUp, ...]:
    """Up to :data:`MAX_FOLLOW_UPS` next requests for a finished turn.

    In order of how much they matter: how the run ended, the checks it did not
    pass, the files it changed, then what its answer offered. Never a
    duplicate, never an empty label or prompt, never a decision-record number.
    """
    return _pick(
        (
            *_after_a_stop(turn),
            *_unmet_requirements(turn),
            *_after_changes(turn),
            *_after_an_answer(turn),
        )
    )


# -- the rules -----------------------------------------------------------------


def _after_a_stop(turn: Turn) -> Iterator[FollowUp]:
    """A run that did not finish: carry on, or try again saying what failed."""
    if turn.succeeded:
        return
    reason = turn.stop_reason
    if reason in _STOPPED_BY_PERSON or reason in _STOPPED_BY_LIMIT:
        yield FollowUp(
            "Carry on from where it stopped",
            "Carry on with this request from where the last run stopped. Some "
            "of it may already be done, so look at the files first.\n\n"
            + turn.prompt,
        )
        return
    # A fault, not a choice: the same request again is the retry.
    yield FollowUp(f"Try again: {_what_failed(turn)}", turn.prompt)


def _unmet_requirements(turn: Turn) -> Iterator[FollowUp]:
    """A check that failed, or one a finished run never verified (DR-038).

    In a run that stopped, a requirement still "Implemented" is unverified
    only because the run stopped — carrying on covers it. One the validator
    marked "Failed" is specific whatever the ending, and its evidence says
    what was wrong, which is what the next run most needs to know.
    """
    failed = [item for item in turn.requirements if item.get("status") == "Failed"]
    unverified = [
        item
        for item in turn.requirements
        if item.get("status") not in ("Verified", "Failed")
    ]
    chosen = failed + (unverified if turn.succeeded else [])
    for item in chosen[:_MAX_REQUIREMENTS]:
        description = " ".join(str(item.get("description", "")).split())
        if not description:
            continue
        evidence = " ".join(str(item.get("evidence", "")).split())
        if item.get("status") == "Failed":
            prompt = f"Finish this requirement, which did not pass its check last time: {description}"
            if evidence:
                prompt += f"\n\nWhat was wrong: {evidence}"
        else:
            prompt = f"Finish this requirement, which was never checked last time: {description}"
        ident = str(item.get("id", "")).strip() or "this"
        yield FollowUp(f"Finish {ident}: {description.replace('`', '')}", prompt)


def _after_changes(turn: Turn) -> Iterator[FollowUp]:
    """Files the run left changed: test them, run the tests, or explain them.

    Only what is still in the workspace counts (DR-024): a change that was
    undone is no longer there to test or explain. After a run that did not
    finish, only the explanation is offered — testing half a change is not
    the next step, understanding it is.
    """
    paths = [change.path for change in turn.changes if change.path]
    if not paths:
        return
    tests = [path for path in paths if _is_test(path)]
    sources = [path for path in paths if path not in tests and not _RECORD_NUMBER.search(path)]
    main = next((path for path in sources if _is_code(path)), sources[0] if sources else "")
    if turn.succeeded:
        if main and _is_code(main) and not tests:
            yield FollowUp(f"Add tests for {_name(main)}", f"Add tests for {main}.")
        if tests or (main and _is_code(main)):
            yield FollowUp("Run the tests", "Run the tests and tell me if anything fails.")
    if main:
        yield FollowUp(
            f"Explain what you changed in {_name(main)}",
            f"Explain what you changed in {main}, and why.",
        )


def _after_an_answer(turn: Turn) -> Iterator[FollowUp]:
    """An answer that changed nothing: make what it proposed, or go deeper.

    "Make this change" relies on the answer being in the model's history,
    which it is only because the run succeeded (DR-004). A deeper question is
    offered only when the answer names a file or a function the request did
    not, so there is something specific to ask about.
    """
    if not turn.succeeded or turn.changes or turn.undone or not turn.answer:
        return
    if _proposes_a_change(turn.answer):
        yield FollowUp("Make this change", "Make the change you proposed in your last answer.")
        return
    asked = turn.prompt.casefold()
    path = next(
        (
            found
            for found in _mentioned_paths(turn.answer)
            if _name(found).casefold() not in asked and not _RECORD_NUMBER.search(found)
        ),
        "",
    )
    if path:
        yield FollowUp(
            f"Walk me through {_name(path)}",
            f"Walk me through {path}: what it does and how it fits in.",
        )
    function = next(
        (
            found
            for found in _MENTIONED_FUNCTION.findall(turn.answer)
            if found.rsplit(".", 1)[-1].casefold() not in asked
        ),
        "",
    )
    if function:
        yield FollowUp(f"Show where {function} is used", f"Show me where {function} is used, and why.")


# -- helpers ---------------------------------------------------------------------


def _pick(candidates: Iterable[FollowUp]) -> tuple[FollowUp, ...]:
    """The first few usable suggestions, each once, with labels kept short."""
    chosen: list[FollowUp] = []
    seen: set[str] = set()
    for candidate in candidates:
        label = _shorten(_without_record_numbers(candidate.label), _LABEL_CHARS)
        prompt = _without_record_numbers(candidate.prompt).strip()
        if not label or not prompt:
            continue
        keys = {label.casefold(), prompt.casefold()}
        if keys & seen:
            continue
        seen |= keys
        chosen.append(FollowUp(label, prompt))
        if len(chosen) == MAX_FOLLOW_UPS:
            break
    return tuple(chosen)


def _what_failed(turn: Turn) -> str:
    if turn.stop_reason == "stage_failed":
        # The failed stage is the last one the route says was started.
        stage = _last_stage(turn.route)
        return f"the {stage} step failed" if stage else "a step failed"
    return _WHAT_FAILED.get(turn.stop_reason) or turn.stop_reason.replace("_", " ") or "it failed"


def _last_stage(route: Sequence[Mapping[str, str]]) -> str:
    for item in reversed(route):
        if item.get("kind") == "stage":
            return item.get("title") or item.get("stage", "").title()
    return ""


def _proposes_a_change(answer: str) -> bool:
    """Whether *answer* offers a change to code rather than making one.

    The phrasing alone is not enough: "I recommend reading the docs" is advice,
    not a change. An offer to change code names some code, so a phrase that
    offers a change counts only in an answer that also has code formatting.
    """
    if "```diff" in answer:
        return True
    return "`" in answer and _PROPOSES_A_CHANGE.search(answer) is not None


def _mentioned_paths(text: str) -> Iterator[str]:
    """Source files *text* names in code formatting, or with a folder."""
    for match in _MENTIONED_PATH.finditer(text):
        found = match.group(1)
        quoted = text[match.start() - 1 : match.start()] == "`" and text[match.end() : match.end() + 1] == "`"
        if quoted or "/" in found:
            yield found


def _is_test(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    stem = name.split(".", 1)[0]
    return (
        any(part.casefold() in _TEST_DIRECTORIES for part in pure.parts[:-1])
        or stem.startswith("test_")
        or stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
        or name == "conftest.py"
        or pure.stem.endswith(("Test", "Tests"))
    )


def _is_code(path: str) -> bool:
    return PurePosixPath(path).suffix.casefold() in _CODE_SUFFIXES


def _name(path: str) -> str:
    """A file's name, for a label; the prompt keeps the whole path."""
    return PurePosixPath(path).name or path


def _without_record_numbers(text: str) -> str:
    return _RECORD_NUMBER.sub("the decision record", text)


def _shorten(text: str, limit: int) -> str:
    """One line of at most *limit* characters, cut at a word."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-—–") + "…"
