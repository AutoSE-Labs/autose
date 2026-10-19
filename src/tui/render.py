"""Turning ``logic`` events and values into terminal output.

Every function here takes data and returns (or prints) Rich renderables. None
of them decide anything: no branching on tier behaviour, no interpretation of
a result beyond how to colour it. That is the DR-002 line — this module knows
what things look like, and nothing about what they mean.
"""

from __future__ import annotations

import textwrap

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from logic.common.approval import EDIT_APPROVAL_TOOLS, ApprovalRequest
from logic.common.changes import FileChange, RevertReport
from logic.followups import FollowUp
from logic.inference.catalog import ModelInfo
from logic.inference.discovery import DetectedRuntime
from logic.persistence import SessionRecord
from logic.session import Session
from logic.tiers import TIER_SPECS, TIERS

from .theme import THEME

__all__ = [
    "banner",
    "stage_header",
    "requirements_table",
    "verdict_panel",
    "loopback_notice",
    "classification_line",
    "outcome_panel",
    "follow_ups_line",
    "changes_summary",
    "diff_panel",
    "revert_report",
    "session_table",
    "runtime_table",
    "model_table",
    "tier_table",
    "tool_table",
    "status_line",
    "approval_panel",
    "APPROVAL_CHOICES",
    "denied_line",
    "thinking_block",
    "thinking_hidden_line",
    "queued_input_line",
    "thinking_summary",
    "reasoning_transcript",
    "warning_text",
    "error_panel",
    "answer_markdown",
    "tool_line",
    "help_table",
    "format_tokens",
]


def banner(version: str) -> RenderableType:
    title = Text()
    title.append("AutoSE", style=f"bold {THEME.accent}")
    title.append(f"  v{version}", style=THEME.muted)
    subtitle = Text(
        "a local-first software engineering agent — your models, your machine",
        style=THEME.muted,
    )
    return Panel(
        Group(title, subtitle),
        border_style=THEME.rule,
        padding=(0, 2),
    )


def runtime_table(runtimes: tuple[DetectedRuntime, ...]) -> RenderableType:
    table = Table(
        title="Local runtimes", title_style=THEME.accent, border_style=THEME.rule
    )
    table.add_column("", width=1)
    table.add_column("Runtime", style=THEME.accent_dim)
    table.add_column("Endpoint", style=THEME.muted)
    table.add_column("Models", justify="right")
    for runtime in runtimes:
        if runtime.usable:
            mark, style = THEME.ok_glyph, THEME.success
            detail = str(len(runtime.models))
        elif runtime.reachable:
            mark, style = THEME.warn_glyph, THEME.warning
            detail = "none"
        else:
            mark, style = THEME.fail_glyph, THEME.muted
            detail = "—"
        table.add_row(
            Text(mark, style=style), runtime.provider, runtime.base_url, detail
        )
    return table


def model_table(models: tuple[ModelInfo, ...], *, current: str = "") -> RenderableType:
    table = Table(border_style=THEME.rule)
    table.add_column("", width=1)
    table.add_column("#", style=THEME.muted, justify="right")
    table.add_column("Model", style=THEME.accent_dim)
    table.add_column("Size", style=THEME.muted)
    table.add_column("Context", justify="right", style=THEME.muted)
    table.add_column("Tools", justify="center")
    for index, model in enumerate(models, start=1):
        if model.supports_tools is True:
            tools = Text(THEME.ok_glyph, style=THEME.success)
        elif model.supports_tools is False:
            tools = Text(THEME.fail_glyph, style=THEME.danger)
        else:
            tools = Text("?", style=THEME.muted)
        table.add_row(
            Text(THEME.bullet if model.name == current else "", style=THEME.accent),
            str(index),
            model.name,
            model.parameter_size or "—",
            _context(model.context_length),
            tools,
        )
    return table


def tier_table(session: Session) -> RenderableType:
    table = Table(border_style=THEME.rule)
    table.add_column("", width=1)
    table.add_column("Tier", style=THEME.accent_dim)
    table.add_column("Workflow")
    table.add_column("Status", style=THEME.muted)
    implemented = set(session.implemented_tiers)
    for tier in TIERS:
        spec = TIER_SPECS[tier]
        ready = tier in implemented
        table.add_row(
            Text(THEME.bullet if tier == session.tier else "", style=THEME.accent),
            tier,
            spec.summary,
            Text("ready", style=THEME.success)
            if ready
            else Text("not implemented yet", style=THEME.warning),
        )
    return table


def tool_table(names: tuple[str, ...], approval_required: frozenset[str]) -> RenderableType:
    table = Table(border_style=THEME.rule)
    table.add_column("Tool", style=THEME.accent_dim)
    table.add_column("Gate", style=THEME.muted)
    for name in names:
        table.add_row(
            name,
            Text("asks first", style=THEME.warning)
            if name in approval_required
            else "runs freely",
        )
    return table


def status_line(session: Session) -> RenderableType:
    """The one-line footer: where we are, what we are using, what it has cost."""
    line = Text()
    line.append(f" {session.inference.model} ", style=f"bold {THEME.accent}")
    line.append(f"{THEME.bullet} {session.tier} ", style=THEME.accent_dim)
    line.append(f"{THEME.bullet} {_short_path(str(session.workspace.root))} ", style=THEME.muted)
    limit = session.context_limit()
    if limit:
        line.append(f"{THEME.bullet} {_context(limit)} ctx ", style=THEME.muted)
    total = session.usage.total_tokens
    if total:
        line.append(f"{THEME.bullet} {format_tokens(total)} tokens", style=THEME.muted)
    return line


#: Every answer the approval prompt accepts, in the order it offers them.
#: Rendered into the panel itself, because a prompt that shows only its
#: default reads as though the default is the only option.
APPROVAL_CHOICES: tuple[tuple[str, str], ...] = (
    ("y", "run it, once"),
    ("n", "skip it"),
    ("a", "allow calls like this for the session"),
)

#: Argument names already carried by ``ApprovalRequest.summary``; showing them
#: a second time as detail rows would just repeat the headline.
#: Arguments the panel already shows above the details block: the one-line
#: summary covers the first three, and the proposed-edit preview covers the
#: rest. Repeating either would show the same content twice.
_SUMMARISED_KEYS: frozenset[str] = frozenset(
    {"command", "url", "query", "path", "edits", "content"}
)


def approval_panel(request: ApprovalRequest) -> RenderableType:
    """DR-012: the user sees the concrete call, not a generic notice."""
    heading = Text()
    heading.append(f"{THEME.approval_glyph} ", style=THEME.warning)
    heading.append(request.tool_name, style=f"bold {THEME.warning}")
    heading.append(" wants to run", style=THEME.muted)

    body: list[RenderableType] = [heading, Text(""), Text(request.summary, style="bold white")]
    proposed = _proposed_edit(request)
    if proposed is not None:
        body.append(Text(""))
        body.append(proposed)
    details = _argument_details(request)
    if details:
        body.append(Text(""))
        body.extend(details)
    body.append(Text(""))
    body.append(approval_choices_line())
    return Panel(Group(*body), border_style=THEME.warning, padding=(0, 2))


def approval_choices_line() -> Text:
    """The legend under the call: every key, spelled out."""
    line = Text()
    for index, (key, meaning) in enumerate(APPROVAL_CHOICES):
        if index:
            line.append("   ", style=THEME.muted)
        line.append(f"[{key}]", style=f"bold {THEME.accent}")
        line.append(f" {meaning}", style=THEME.muted)
    line.append("\n")
    line.append("or type a reason", style=f"bold {THEME.accent}")
    line.append(" to decline and tell the model what to do instead", style=THEME.muted)
    return line


#: How much of a proposed edit is shown before the panel stops being readable.
_EDIT_PREVIEW_LINES = 12


def _proposed_edit(request: ApprovalRequest) -> RenderableType | None:
    """Render what a gated `write_file`/`edit_file` call would actually do.

    Only reached when a workspace opted into gating edits (DR-024). Showing
    the path alone would make the prompt unanswerable — approving a write
    means approving its *content*.
    """
    if request.tool_name not in EDIT_APPROVAL_TOOLS:
        return None

    body = Text()
    edits = request.arguments.get("edits")
    if isinstance(edits, (list, tuple)):
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                continue
            if index:
                body.append("\n")
            _append_block(body, str(edit.get("old_str", "")), "-", THEME.danger)
            _append_block(body, str(edit.get("new_str", "")), "+", THEME.success)
        return body if body.plain else None

    content = request.arguments.get("content")
    if isinstance(content, str) and content:
        _append_block(body, content, "+", THEME.success)
        return body
    return None


def _append_block(body: Text, block: str, marker: str, style: str) -> None:
    lines = block.splitlines() or [""]
    for line in lines[:_EDIT_PREVIEW_LINES]:
        body.append(f"{marker} {line}\n", style=style)
    if len(lines) > _EDIT_PREVIEW_LINES:
        body.append(
            f"{marker} … {len(lines) - _EDIT_PREVIEW_LINES} more line(s)\n",
            style=THEME.muted,
        )


def _argument_details(request: ApprovalRequest) -> list[Text]:
    """The arguments the one-line summary does not already show."""
    rows: list[Text] = []
    for key, value in sorted(request.arguments.items()):
        if key in _SUMMARISED_KEYS or value in (None, "", [], {}):
            continue
        row = Text()
        row.append(f"{key}: ", style=THEME.muted)
        row.append(_truncate(str(value), 80), style=THEME.accent_dim)
        rows.append(row)
    return rows


def denied_line(reason: str = "") -> RenderableType:
    """What the transcript keeps after a refusal — including any guidance."""
    line = Text()
    line.append(f"  {THEME.fail_glyph} denied", style=THEME.danger)
    if reason:
        line.append(f"  {_truncate(reason, 96)}", style=THEME.muted)
    return line


def tool_line(name: str, detail: str = "") -> RenderableType:
    line = Text()
    line.append(f"{THEME.tool_glyph} ", style=THEME.tool)
    line.append(name, style=THEME.tool)
    if detail:
        line.append(f"  {_truncate(detail, 100)}", style=THEME.muted)
    return line


def warning_text(message: str) -> RenderableType:
    line = Text()
    line.append(f"{THEME.warn_glyph} ", style=THEME.warning)
    line.append(message, style=THEME.warning)
    return line


def error_panel(message: str, *, title: str = "Error") -> RenderableType:
    return Panel(Text(message, style=THEME.danger), title=title, border_style=THEME.danger)


def answer_markdown(text: str) -> RenderableType:
    return Markdown(text or "_(no answer)_")


def help_table(commands: dict[str, str]) -> RenderableType:
    table = Table(border_style=THEME.rule, title="Commands", title_style=THEME.accent)
    table.add_column("Command", style=THEME.accent_dim)
    table.add_column("What it does")
    for name, description in commands.items():
        table.add_row(name, description)
    return table


# -- the thinking block ------------------------------------------------------


def thinking_block(
    text: str,
    *,
    width: int,
    max_lines: int,
    expanded: bool = False,
    label: str = "thinking",
    hint: str = "",
) -> RenderableType:
    """The model's chain of thought as a bounded, scrollable-looking block.

    A single truncated line that rewrites itself is unreadable: by the time
    the eye reaches the end of a sentence the start of it is gone. This keeps
    the last *max_lines* wrapped lines visible in a gutter-marked block, and
    says plainly that the whole thing is retrievable — DR-016 makes reasoning
    telemetry, and telemetry the user cannot read is just noise.
    """
    body_width = max(20, width - 6)
    lines = _tail_lines(text, body_width, max_lines)
    header = Text()
    header.append(f"  {'▾' if expanded else '▸'} {label}", style=THEME.thinking)
    header.append(f"  {_abbreviate(len(text))} chars", style=THEME.muted)
    if hint:
        header.append(f"  {hint}", style=THEME.muted)
    rendered: list[RenderableType] = [header]
    for line in lines:
        row = Text()
        row.append("  │ ", style=THEME.rule)
        row.append(line, style=THEME.thinking)
        rendered.append(row)
    return Group(*rendered)


def thinking_hidden_line(hint: str = "") -> RenderableType:
    """One line standing in for a hidden block, so it can be found again."""
    line = Text()
    line.append("  ▪ thinking hidden", style=THEME.thinking)
    if hint:
        line.append(f"  {hint}", style=THEME.muted)
    return line


def queued_input_line(text: str, marker: str = "⋮") -> RenderableType:
    """What the user has typed while the model works.

    Keys are read one at a time during a turn, which means the terminal is not
    echoing them. Showing the buffer back is what keeps typing ahead from
    being typing blind — it lands in the next prompt, ready to edit.
    """
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(f"  {marker} ", style=THEME.accent_dim)
    line.append(text, style=THEME.user)
    line.append("  (goes to the next prompt)", style=THEME.muted)
    return line


def thinking_summary(chars: int, seconds: float, label: str = "thought") -> RenderableType:
    """The one line the transcript keeps once a thought is finished."""
    line = Text()
    line.append(f"  {THEME.think_glyph} {label}", style=THEME.thinking)
    line.append(
        f" for {seconds:.0f}s  {THEME.bullet} {_abbreviate(chars)} chars"
        f"  {THEME.bullet} /thinking to read it",
        style=THEME.muted,
    )
    return line


def reasoning_transcript(segments: tuple[tuple[str, str], ...]) -> RenderableType:
    """Every thought from the last turn, in full, on demand."""
    if not segments:
        return Text("  nothing was reasoned aloud in the last turn", style=THEME.muted)
    blocks: list[RenderableType] = []
    for label, body in segments:
        if blocks:
            blocks.append(Text(""))
        if len(segments) > 1:
            # One thought needs no heading; several — a staged run's stages —
            # are unreadable without one.
            blocks.append(Text(label, style=f"bold {THEME.accent}"))
        blocks.append(Text(body.strip(), style=THEME.thinking))
    return Panel(
        Group(*blocks),
        title="thinking",
        title_align="left",
        border_style=THEME.rule,
        padding=(0, 2),
    )


def _tail_lines(text: str, width: int, count: int) -> list[str]:
    """The last *count* display lines of *text*, wrapped to *width*."""
    if count <= 0 or not text:
        return []
    # Wrapping the whole of a long chain of thought on every repaint is waste:
    # only the tail can possibly be shown.
    tail = text[-(count * width * 4) :]
    lines: list[str] = []
    for paragraph in tail.splitlines():
        stripped = paragraph.strip()
        if stripped:
            lines.extend(textwrap.wrap(stripped, width) or [stripped[:width]])
    return lines[-count:]


def _abbreviate(count: int) -> str:
    return format_tokens(count)


# -- formatting helpers ------------------------------------------------------


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _context(tokens: int | None) -> str:
    if not tokens:
        return "—"
    return f"{tokens // 1024}k" if tokens >= 1024 else str(tokens)


def _short_path(path: str, limit: int = 40) -> str:
    if len(path) <= limit:
        return path
    parts = path.split("/")
    return ".../" + "/".join(parts[-2:]) if len(parts) > 2 else path[-limit:]


def _truncate(text: str, limit: int) -> str:
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


# -- multi-stage rendering ---------------------------------------------------


def classification_line(tier: str, source: str, ratcheted_to: str = "") -> RenderableType:
    """How the tier for this prompt was chosen (DR-003)."""
    line = Text()
    line.append(f"  {THEME.bullet} ", style=THEME.muted)
    if ratcheted_to and ratcheted_to != tier:
        # The ratchet held the session at a higher tier than this prompt asked
        # for; say so, or the tier looks wrong.
        line.append(f"{tier}", style=THEME.muted)
        line.append(" → ", style=THEME.muted)
        line.append(ratcheted_to, style=THEME.accent)
        line.append("  (session tier only ratchets up)", style=THEME.muted)
    else:
        line.append(tier, style=THEME.accent)
    if source != "model":
        line.append(f"  [{source}]", style=THEME.warning)
    return line


def stage_header(
    stage: str, title: str, iteration: int = 1, *, total_stages: int = 0, position: int = 0
) -> RenderableType:
    """A rule announcing a stage, so the transcript is scannable."""
    label = Text()
    label.append(f" {title} ", style=f"bold {THEME.accent}")
    if position and total_stages:
        label.append(f"{position}/{total_stages} ", style=THEME.muted)
    if iteration > 1:
        label.append(f"attempt {iteration} ", style=THEME.warning)
    return Panel(label, border_style=THEME.rule, padding=(0, 0), expand=False)


def requirements_table(requirements: list[dict]) -> RenderableType:
    """The ledger, which is the only trustworthy answer to 'is this done'."""
    table = Table(border_style=THEME.rule, show_header=True, box=None, padding=(0, 1))
    table.add_column("", style=THEME.muted, width=4)
    table.add_column("Requirement")
    table.add_column("Status", width=12)
    table.add_column("Evidence", style=THEME.muted, overflow="ellipsis", max_width=34)
    for item in requirements:
        table.add_row(
            item.get("id", ""),
            item.get("description", ""),
            _status_text(item.get("status", "")),
            item.get("evidence", ""),
        )
    return table


def _status_text(status: str) -> Text:
    style = {
        "Verified": THEME.success,
        "Implemented": THEME.accent_dim,
        "In Progress": THEME.warning,
        "Blocked": THEME.danger,
        "Failed": THEME.danger,
        "Not Started": THEME.muted,
    }.get(status, THEME.muted)
    return Text(status, style=style)


def verdict_panel(data: dict) -> RenderableType:
    """The validation outcome, including whether the runner overrode it."""
    if data.get("passed"):
        body = Text()
        body.append(f"{THEME.ok_glyph} passed", style=f"bold {THEME.success}")
        body.append(f"  {data.get('requirements', '')}", style=THEME.muted)
        return Panel(body, border_style=THEME.success, padding=(0, 2), expand=False)

    lines: list[RenderableType] = []
    heading = Text()
    if data.get("overridden"):
        # DR-005 in action: worth showing plainly, because it is the whole
        # point of not trusting the stage's self-report.
        heading.append(f"{THEME.warn_glyph} claimed pass overridden", style=f"bold {THEME.warning}")
        heading.append("  the runner re-checked the ledger", style=THEME.muted)
    else:
        heading.append(f"{THEME.fail_glyph} sent back", style=f"bold {THEME.warning}")
        heading.append(f" to {data.get('target', '?')}", style=THEME.muted)
    lines.append(heading)
    if data.get("unmet"):
        lines.append(
            Text(f"  unverified: {', '.join(data['unmet'])}", style=THEME.danger)
        )
    if data.get("feedback"):
        lines.append(Text(""))
        lines.append(Text(data["feedback"], style=THEME.assistant))
    return Panel(Group(*lines), border_style=THEME.warning, padding=(0, 2))


def loopback_notice(data: dict) -> RenderableType:
    line = Text()
    line.append(f"  ↻ back to {data.get('target', '?')}", style=THEME.warning)
    line.append(
        f"  (attempt {data.get('iteration', 0)} of {data.get('cap', 0)})",
        style=THEME.muted,
    )
    return line


#: How a change's kind reads in the summary, and in what colour.
_KIND_STYLES: dict[str, tuple[str, str]] = {
    "created": ("new", THEME.success),
    "modified": ("changed", THEME.accent_dim),
    "deleted": ("removed", THEME.danger),
}


def changes_summary(changes: tuple[FileChange, ...]) -> RenderableType:
    """What a turn wrote, with the size of each change (DR-024).

    The line counts are the point: "changed 3 files" and "changed 3 files,
    +412 −180" call for different amounts of attention, and the second is the
    one worth reading before moving on.
    """
    table = Table(border_style=THEME.rule, box=None, show_header=False, padding=(0, 1))
    table.add_column("", width=2)
    table.add_column("")
    table.add_column("", justify="right")
    table.add_column("")

    for change in changes:
        added, removed = change.stats
        counts = Text()
        if added:
            counts.append(f"+{added}", style=THEME.success)
        if added and removed:
            counts.append(" ")
        if removed:
            counts.append(f"−{removed}", style=THEME.danger)
        if not change.restorable:
            counts = Text("unrecorded", style=THEME.warning)

        label, style = _KIND_STYLES.get(change.kind, ("changed", THEME.accent_dim))
        table.add_row(
            Text(THEME.bullet, style=THEME.success),
            Text(change.path, style=THEME.accent_dim),
            counts,
            Text(label, style=style),
        )

    hint = Text("  /diff to review, /undo to put it back", style=THEME.muted)
    return Group(table, hint)


def diff_panel(changes: tuple[FileChange, ...], *, context: int = 3) -> RenderableType:
    """Every change as a coloured unified diff."""
    if not changes:
        return Text("  nothing has been changed in this session", style=THEME.muted)

    blocks: list[RenderableType] = []
    for change in changes:
        if blocks:
            blocks.append(Text(""))
        blocks.append(_diff_text(change, context=context))
    return Panel(
        Group(*blocks),
        title=_diff_title(changes),
        border_style=THEME.rule,
        padding=(0, 2),
    )


def _diff_title(changes: tuple[FileChange, ...]) -> str:
    added = sum(change.stats[0] for change in changes)
    removed = sum(change.stats[1] for change in changes)
    files = f"{len(changes)} file{'s' if len(changes) != 1 else ''}"
    return f"{files}, +{added} −{removed}"


def _diff_text(change: FileChange, *, context: int = 3) -> Text:
    body = Text()
    for line in change.diff(context=context).splitlines():
        body.append(line + "\n", style=_diff_style(line))
    return body


def _diff_style(line: str) -> str:
    if line.startswith(("+++", "---")):
        return THEME.muted
    if line.startswith("@@"):
        return THEME.accent
    if line.startswith("+"):
        return THEME.success
    if line.startswith("-"):
        return THEME.danger
    if line.startswith("#"):
        return THEME.warning
    return THEME.assistant


def revert_report(report: RevertReport) -> RenderableType:
    """What an undo restored, and what it deliberately would not touch."""
    lines: list[RenderableType] = []
    for path in report.restored:
        line = Text()
        line.append(f"  {THEME.ok_glyph} ", style=THEME.success)
        line.append(path, style=THEME.accent_dim)
        line.append("  restored", style=THEME.muted)
        lines.append(line)
    for path, reason in report.skipped:
        line = Text()
        line.append(f"  {THEME.fail_glyph} ", style=THEME.warning)
        line.append(path, style=THEME.accent_dim)
        line.append(f"  left alone — {reason}", style=THEME.warning)
        lines.append(line)
    if not lines:
        return Text("  there was nothing to undo", style=THEME.muted)
    return Group(*lines)


def session_table(records: tuple[SessionRecord, ...], *, current: str = "") -> RenderableType:
    """Stored sessions, newest first, with what each one was about (DR-026)."""
    if not records:
        return Text(
            "  no stored sessions for this workspace yet", style=THEME.muted
        )

    table = Table(border_style=THEME.rule, box=None, padding=(0, 2))
    table.add_column("", width=1)
    table.add_column("id", style=THEME.accent_dim)
    table.add_column("when", style=THEME.muted)
    table.add_column("turns", justify="right", style=THEME.muted)
    table.add_column("model", style=THEME.muted)
    table.add_column("first request", style=THEME.assistant)

    for record in records:
        marker = THEME.bullet if record.id == current else " "
        title = Text(record.title)
        if record.queue:
            # DR-056: requests the dashboard queued wait there, held, until
            # someone resumes them; a terminal resume keeps them untouched.
            title.append(
                f"  · {len(record.queue)} queued in the dashboard", style=THEME.muted
            )
        table.add_row(
            Text(marker, style=THEME.success),
            record.id,
            _ago(record.updated_at),
            str(len(record.turns)),
            record.model or "—",
            title,
        )
    return Group(
        table, Text("  autose --resume <id> to pick one up", style=THEME.muted)
    )


def _ago(timestamp: float) -> str:
    """A relative time. Absolute dates make a list of sessions unreadable."""
    import time

    if not timestamp:
        return "—"
    seconds = max(0, int(time.time() - timestamp))
    for size, label in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{label} ago"
    return "just now"


#: How each stop reason is titled, and what to say when the run stopped before
#: it had written anything worth showing. DR-030 makes ``cancelled`` the
#: ordinary end of a long run, so it is titled as a choice rather than a fault.
_OUTCOME_TITLES: dict[str, str] = {
    "attempts_exhausted": "Out of attempts",
    "time_budget_exhausted": "Out of time",
    "stage_failed": "Stage failed",
    "round_cap": "Stopped early",
    "stalled": "Stopped going in circles",
    "context_thrashing": "Context window too small",
    "cancelled": "Stopped by you",
    "empty_answer": "No answer produced",
    "no_result": "Nothing produced",
}

_OUTCOME_FALLBACKS: dict[str, str] = {
    "cancelled": "Stopped before there was anything to show.",
    "round_cap": "The round limit was reached before an answer was written.",
    "stalled": (
        "It kept re-reading what it had already been given instead of answering, "
        "so it was stopped. This usually means the model is too small for the "
        "request — try a larger one, or ask something narrower."
    ),
    "context_thrashing": (
        "Every round had to discard the evidence the round before it gathered, "
        "to fit the served context window — so the model kept re-reading what "
        "it could no longer see, and was stopped. The window is the problem, "
        "not the model: raise it and run this again."
    ),
    "empty_answer": "The model returned nothing at all.",
    "no_result": "The run ended without producing a result.",
}

#: What to say instead of the text when the partial answer has *already* been
#: streamed to the screen. Printing it again inside the panel reads as two
#: answers, one of which the user has to work out is the same one.
_OUTCOME_ALREADY_SHOWN: dict[str, str] = {
    "cancelled": "Stopped at your request — what is above is as far as it got.",
    "round_cap": "The round limit stopped it — what is above is as far as it got.",
    "stalled": (
        "It stopped making progress — what is above is as far as it got."
    ),
    "context_thrashing": (
        "The context window could not hold what it read — what is above is as "
        "far as it got."
    ),
}


def outcome_panel(
    stop_reason: str, message: str, *, already_shown: bool = False
) -> RenderableType:
    """A workflow that stopped without completing, never dressed as success.

    *already_shown* says the text in *message* has been rendered above this
    panel — which is the ordinary case for a single-pass tier, where the answer
    streams as it is produced.
    """
    text = message.strip()
    if text and already_shown:
        body = _OUTCOME_ALREADY_SHOWN.get(
            stop_reason, "It did not complete — what is above is as far as it got."
        )
    else:
        body = text or _OUTCOME_FALLBACKS.get(stop_reason, "The run did not complete.")
    return Panel(
        Text(body, style=THEME.warning),
        title=_OUTCOME_TITLES.get(stop_reason, "Not completed"),
        border_style=THEME.warning,
    )


def follow_ups_line(follow_ups: tuple[FollowUp, ...]) -> RenderableType | None:
    """What to ask next, as one dim line under the result (DR-063).

    A hint rather than a menu: the terminal has nothing to click, so it names
    the requests and the person types the one they want. Dim, because it is
    the least important thing on the screen once a run has ended.
    """
    if not follow_ups:
        return None
    line = Text(style=THEME.muted)
    line.append("  next: ")
    line.append(f"  {THEME.bullet}  ".join(item.label for item in follow_ups))
    return line
