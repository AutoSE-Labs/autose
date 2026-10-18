"""Slash commands.

Each handler translates a typed line into calls against the ``Session``
facade and renders the outcome. They contain no logic of their own: choosing a
model, changing tier, and listing tools are all `logic/` operations, invoked
here and displayed here (DR-002).
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

from logic.common.approval import APPROVAL_REQUIRED_TOOLS
from logic.errors import AutoSEError
from logic.tiers import TIERS, is_tier

from .render import (
    changes_summary,
    diff_panel,
    error_panel,
    help_table,
    model_table,
    reasoning_transcript,
    revert_report,
    runtime_table,
    session_table,
    tier_table,
    tool_table,
    warning_text,
)
from .theme import THEME

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

__all__ = ["COMMANDS", "dispatch", "command_help", "is_command"]


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: Callable[[App, str], bool]


def is_command(line: str) -> bool:
    return line.startswith("/")


def dispatch(app: App, line: str) -> bool:
    """Run a slash command. Returns False when the session should end."""
    name, _, argument = line[1:].partition(" ")
    typed = name.strip().lower()
    command = COMMANDS.get(typed)
    if command is None:
        app.console.print(warning_text(f"unknown command '/{name}'. Try /help."))
        for suggestion in difflib.get_close_matches(typed, COMMANDS, n=2, cutoff=0.5):
            app.console.print(
                Text(f"  did you mean /{suggestion}?", style=THEME.muted)
            )
        return True
    try:
        return command.handler(app, argument.strip())
    except AutoSEError as exc:
        app.console.print(error_panel(str(exc)))
        return True


def command_help() -> dict[str, str]:
    return {f"/{command.name}": command.description for command in COMMANDS.values()}


# -- handlers ---------------------------------------------------------------


#: Keys and gestures that are not slash commands, and so would otherwise be
#: undiscoverable. A feature nobody can find is a feature nobody has.
_TIPS: tuple[str, ...] = (
    "end a line with \\ to keep typing on the next one",
    "ctrl-o while the model works: expand, hide, restore the thinking block",
    "at an approval prompt, type a reason instead of y/n to redirect the model",
    "ctrl-c stops the run and keeps what it produced (again to abort), "
    "ctrl-d exits",
)


def _help(app: App, argument: str) -> bool:
    app.console.print(help_table(command_help()))
    for tip in _TIPS:
        app.console.print(Text(f"  {THEME.bullet} {tip}", style=THEME.muted))
    return True


def _thinking(app: App, argument: str) -> bool:
    """Read, or stop showing, the model's chain of thought (DR-016)."""
    activity = getattr(app, "activity", None)
    if activity is None:
        app.console.print(warning_text("no activity indicator is attached"))
        return True

    option = argument.strip().lower()
    if not option:
        app.console.print(reasoning_transcript(activity.thoughts))
        return True
    if option in ("on", "show"):
        activity.show_thinking = True
        app.console.print(Text("  thinking is shown while it streams", style=THEME.muted))
    elif option in ("off", "hide"):
        activity.show_thinking = False
        app.console.print(
            Text(
                "  thinking is hidden — /thinking still prints it afterwards",
                style=THEME.muted,
            )
        )
    elif option in ("expand", "full", "more"):
        activity.show_thinking = True
        activity.expanded = True
        app.console.print(Text("  the live thinking block is expanded", style=THEME.muted))
    elif option in ("collapse", "less"):
        activity.expanded = False
        app.console.print(Text("  the live thinking block is collapsed", style=THEME.muted))
    else:
        app.console.print(
            warning_text(
                f"'{option}' is not an option. Use /thinking, or "
                "/thinking on|off|expand|collapse"
            )
        )
    return True


def _quit(app: App, argument: str) -> bool:
    return False


def _models(app: App, argument: str) -> bool:
    """List models, or switch to one by number or name."""
    models = app.session.available_models()
    if not models:
        app.console.print(
            warning_text("could not list models — is the runtime still reachable?")
        )
        return True

    if not argument:
        app.console.print(model_table(models, current=app.session.inference.model))
        app.console.print(
            Text("  /model <number|name> to switch", style=THEME.muted)
        )
        return True

    chosen = _resolve_model(argument, [model.name for model in models])
    if chosen is None:
        app.console.print(warning_text(f"no model matching '{argument}'"))
        return True

    app.session.select_model(chosen)
    app.approval_gate.reset()
    info = app.session.model_info
    app.console.print(
        Text(f"  {THEME.ok_glyph} now using {chosen}", style=THEME.success)
    )
    if info is not None and info.supports_tools is False:
        app.console.print(
            warning_text(f"'{chosen}' reports no tool-calling support")
        )
    return True


def _tier(app: App, argument: str) -> bool:
    if not argument:
        app.console.print(tier_table(app.session))
        app.console.print(
            Text(
                f"  /tier <{'|'.join(TIERS)}>: automatic classification only moves "
                "up; a tier you choose here is used as chosen",
                style=THEME.muted,
            )
        )
        return True

    requested = argument.strip().lower()
    if not is_tier(requested):
        app.console.print(
            warning_text(f"'{requested}' is not a tier. Choose from: {', '.join(TIERS)}")
        )
        return True

    previous = app.session.tier
    effective = app.session.choose_tier(requested)
    if effective != previous:
        app.console.print(
            Text(f"  {THEME.ok_glyph} tier is now {effective}", style=THEME.success)
        )
    if effective not in app.session.implemented_tiers:
        # Unreachable today: every tier has a registered workflow. Kept so a
        # tier added to logic.tiers ahead of its workflow reports that plainly
        # rather than failing when the user next sends a prompt.
        app.console.print(
            warning_text(f"the '{effective}' workflow is not implemented yet")
        )
    return True


def _tools(app: App, argument: str) -> bool:
    from logic.workflows.spark import SPARK_TOOL_NAMES

    app.console.print(tool_table(SPARK_TOOL_NAMES, APPROVAL_REQUIRED_TOOLS))
    allowed = app.approval_gate.always_allowed
    if allowed:
        app.console.print(
            Text(
                f"  allowed for this session: {', '.join(sorted(allowed))}",
                style=THEME.muted,
            )
        )
    # DR-057: rules for every session here, added from the dashboard.
    everywhere = app.approval_gate.workspace_allowed
    if everywhere:
        app.console.print(
            Text(
                f"  allowed in every session in this workspace: {', '.join(sorted(everywhere))}",
                style=THEME.muted,
            )
        )
    return True


def _runtimes(app: App, argument: str) -> bool:
    from logic.inference.discovery import discover_local_runtimes

    app.console.print(runtime_table(discover_local_runtimes(include_unreachable=True)))
    return True


def _clear(app: App, argument: str) -> bool:
    app.session.clear_history()
    app.console.print(Text("  history cleared", style=THEME.muted))
    return True


def _status(app: App, argument: str) -> bool:
    app.print_status()
    return True


def _config(app: App, argument: str) -> bool:
    """Write this workspace's config file, or say where it already is.

    DR-021 puts every tunable and every built-in prompt in a workspace config,
    which is only useful if a user can get one. The file is written at its real
    defaults, so editing it changes behaviour and deleting a key restores the
    built-in.
    """
    from logic.config import CONFIG_FILENAME, write_example_config

    target = app.session.workspace.root / ".autose" / CONFIG_FILENAME
    if target.exists():
        app.console.print(
            Text(f"  config already exists: {target}", style=THEME.muted)
        )
        return True
    try:
        written = write_example_config(target)
    except AutoSEError as exc:
        app.console.print(error_panel(str(exc)))
        return True
    app.console.print(Text(f"  wrote {written}", style=THEME.success))
    app.console.print(
        Text(
            "  every setting is at its current default — edit what you need, "
            "delete a key to restore it",
            style=THEME.muted,
        )
    )
    return True


def _diff(app: App, argument: str) -> bool:
    """Show what this session has written, as a unified diff (DR-024)."""
    changes = _session_changes(app)
    if not changes:
        app.console.print(
            Text("  nothing has been changed in this session", style=THEME.muted)
        )
        return True
    app.console.print(diff_panel(changes))
    return True


def _undo(app: App, argument: str) -> bool:
    """Put the workspace back as it was before the last turn that wrote."""
    report = app.session.undo_last_change()
    if report is None:
        app.console.print(Text("  there is nothing to undo", style=THEME.muted))
        _mention_resumable_changes(app)
        return True
    app.console.print(revert_report(report))
    if report.skipped:
        app.console.print(
            warning_text(
                "a file AutoSE wrote has changed since — it was left alone rather "
                "than overwritten. Review it with /diff, or edit it yourself."
            )
        )
    return True


def _mention_resumable_changes(app: App) -> None:
    """Point at a stored session that *does* have something to undo (DR-026).

    Undo is scoped to the session that made the change, which is correct — but
    a fresh session in a workspace AutoSE worked on yesterday would otherwise
    say "nothing to undo" while a revertible turn sits in the store, and read
    as though the record had been lost.
    """
    try:
        stored = [
            record
            for record in app.session.store.list(limit=5)
            if record.id != app.session.session_id
            and any(turn.changes for turn in record.turns)
        ]
    except Exception:  # noqa: BLE001 - a hint must never fail a command
        return
    if not stored:
        return
    latest = stored[0]
    app.console.print(
        Text(
            f"  an earlier session ({latest.id}) still has changes on record — "
            f"`autose --resume {latest.id}` to review or undo them",
            style=THEME.muted,
        )
    )


def _changes(app: App, argument: str) -> bool:
    """List what this session has written, without the full diff."""
    changes = _session_changes(app)
    if not changes:
        app.console.print(
            Text("  nothing has been changed in this session", style=THEME.muted)
        )
        return True
    app.console.print(changes_summary(changes))
    return True


def _sessions(app: App, argument: str) -> bool:
    """List the sessions stored for this workspace (DR-026)."""
    records = app.session.store.list(limit=20)
    app.console.print(session_table(records, current=app.session.session_id))
    for problem in getattr(app.session.store, "problems", ()):
        app.console.print(warning_text(problem))
    return True


def _session_changes(app: App) -> tuple:
    """Every file this session wrote, newest turn first, one entry per path."""
    seen: dict[str, object] = {}
    for turn in reversed(app.session.history):
        for change in turn.changes:
            seen.setdefault(change.path, change)
    return tuple(seen.values())


COMMANDS: dict[str, Command] = {
    command.name: command
    for command in (
        Command("help", "show this list", _help),
        Command("model", "list models, or switch: /model 2", _models),
        Command("tier", "show tiers, or request one: /tier craft", _tier),
        Command("tools", "show this tier's tools and which need approval", _tools),
        Command("changes", "list the files this session has written", _changes),
        Command("diff", "review what AutoSE wrote, as a unified diff", _diff),
        Command("undo", "put back what the last turn that wrote changed", _undo),
        Command(
            "thinking",
            "read the last turn's reasoning (ctrl-o resizes it live)",
            _thinking,
        ),
        Command("runtimes", "re-probe for local model runtimes", _runtimes),
        Command("sessions", "list stored sessions for this workspace", _sessions),
        Command("status", "show the current model, tier, workspace and usage", _status),
        Command("config", "write this workspace's config file, at its defaults", _config),
        Command("clear", "forget the conversation history", _clear),
        Command("quit", "exit AutoSE", _quit),
    )
}


def _resolve_model(argument: str, names: list[str]) -> str | None:
    """Accept a 1-based index, an exact name, or an unambiguous prefix."""
    if argument.isdigit():
        index = int(argument)
        return names[index - 1] if 1 <= index <= len(names) else None
    if argument in names:
        return argument
    matches = [name for name in names if name.startswith(argument)]
    return matches[0] if len(matches) == 1 else None
