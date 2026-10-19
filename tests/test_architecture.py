"""Executable checks for the boundaries DR-002, DR-003, and DR-013 fix.

These are cheap to run and catch the failure modes those DRs exist to prevent
— a logic module reaching into a UI, orchestration reappearing as a sibling of
``logic``, or an old tier name creeping back in — at the moment they are
introduced rather than at review time.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
LOGIC = SRC / "logic"


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _python_files(LOGIC), ids=lambda p: p.name)
def test_logic_never_imports_an_adapter(path):
    """DR-002: dependencies point one way — adapters depend on logic, never back."""
    offenders = {
        module
        for module in _imported_modules(path)
        if module == "tui"
        or module == "gui"
        or module.startswith(("tui.", "gui."))
    }
    assert not offenders, f"{path.relative_to(SRC)} imports adapter module(s) {offenders}"


def test_src_has_only_the_three_decided_top_level_packages():
    """DR-002: orchestration lives inside logic, not as a sibling of it."""
    # Only real packages count: build metadata such as *.egg-info lands here
    # too and is not part of the layout decision.
    packages = {
        entry.name
        for entry in SRC.iterdir()
        if entry.is_dir() and (entry / "__init__.py").is_file()
    }
    assert packages <= {"logic", "tui", "gui"}, f"unexpected top-level package(s): {packages}"


def test_no_orchestration_sibling_reappears():
    for forbidden in ("core", "runners", "orchestration"):
        assert not (SRC / forbidden).exists(), (
            f"src/{forbidden}/ is orchestration, which DR-002 places inside src/logic/"
        )


def _identifiers_and_literals(path: Path) -> set[str]:
    """Every name and string literal in *path*, ignoring prose in docstrings.

    Prose is excluded deliberately: "the standard library" is not a tier name,
    and a check that cannot tell the difference gets disabled the first time it
    cries wolf.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
            found.add(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ):
            found.add(node.value)
    return found


@pytest.mark.parametrize(
    "retired", ["lite", "standard", "advanced"], ids=["lite", "standard", "advanced"]
)
def test_retired_tier_names_do_not_reappear(retired):
    """DR-003 renamed the tiers to spark/craft(/forge in Pro); the old names are gone.

    Matching is against identifiers and non-docstring string literals, split on
    the separators a tier name would actually appear across, so ``advanced``
    the tier is caught while "advanced usage" in a comment is not.
    """
    token = re.compile(rf"(?:^|[^a-z]){retired}(?:$|[^a-z])", re.IGNORECASE)
    offenders = {
        f"{path.relative_to(SRC)}: {value!r}"
        for path in _python_files(SRC)
        for value in _identifiers_and_literals(path)
        if token.search(value)
    }
    assert not offenders, f"retired tier name '{retired}' found at {sorted(offenders)}"


def test_the_gui_adapter_is_separable_from_logic_and_tui():
    """DR-013: a public-edition build drops src/gui with nothing else changing."""
    for package in ("logic", "tui"):
        root = SRC / package
        if not root.exists():
            continue
        for path in _python_files(root):
            assert "gui" not in _imported_modules(path), (
                f"{path.relative_to(SRC)} imports gui, so a public build could not "
                "exclude src/gui/"
            )


# -- Decision Record numbers are for developers, not for people using AutoSE --


def _code_without_comments(text: str) -> str:
    import re

    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", line) for line in text.splitlines())


def test_the_dashboard_never_shows_a_decision_record_number():
    """"(DR-024)" means something to a contributor reading the code and nothing
    to someone using the app. DR numbers belong in comments and docs only."""
    import re

    static = SRC / "gui" / "static"
    if not static.exists():
        return
    for path in sorted(static.glob("*.*")):
        if path.suffix not in {".js", ".html", ".css"}:
            continue
        visible = _code_without_comments(path.read_text(encoding="utf-8"))
        found = re.findall(r"DR-\d{3}", visible)
        assert not found, f"{path.name} shows {sorted(set(found))} to users"


def test_no_message_or_generated_file_mentions_a_decision_record_number():
    """Errors, warnings, terminal output and the config file AutoSE writes are
    read by users. Docstrings and comments are where DR numbers belong."""
    import ast
    import re

    pattern = re.compile(r"DR-\d{3}")
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and pattern.search(node.value)
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, f"user-visible text mentions decision records: {offenders}"
