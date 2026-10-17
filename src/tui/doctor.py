"""Renders ``logic.diagnostics`` for a terminal."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from logic.diagnostics import diagnose

from .render import model_table, runtime_table, warning_text
from .theme import THEME

__all__ = ["print_report"]


def print_report(console: Console | None = None) -> int:
    """Print what this machine can run. Returns a process exit code."""
    console = console or Console(highlight=False)
    with console.status(Text("probing…", style=THEME.muted), spinner="dots"):
        diagnosis = diagnose()

    console.print()
    console.print(
        Text(f"  config: {diagnosis.config.describe_source()}", style=THEME.muted)
    )
    console.print(
        Text(
            f"  workspace: {diagnosis.config.resolve_workspace_root()}",
            style=THEME.muted,
        )
    )
    console.print(
        Text(
            f"  tiers implemented: {', '.join(diagnosis.implemented_tiers) or 'none'}",
            style=THEME.muted,
        )
    )
    console.print()
    console.print(runtime_table(diagnosis.runtimes))

    for runtime in diagnosis.usable_runtimes:
        console.print()
        console.print(Text(f"  {runtime.provider}", style=THEME.accent))
        console.print(model_table(runtime.models))

    # DR-022: shown whether or not the setup is healthy — a capped window does
    # not stop AutoSE working, and is invisible everywhere else.
    for advisory in diagnosis.advisories:
        console.print()
        console.print(warning_text(advisory))

    console.print()
    if diagnosis.healthy:
        console.print(
            Text(
                f"  {THEME.ok_glyph} ready — {diagnosis.total_models} model(s) available",
                style=THEME.success,
            )
        )
        return 0
    for problem in diagnosis.problems:
        console.print(warning_text(problem))
    return 1
