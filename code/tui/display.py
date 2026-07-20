"""
Shared TUI display primitives for AutoSE.

Layout (bottom-to-top):
  ┌─────────────────────────────────────────────┐
  │  Chat window (scrollable history)           │
  ├─────────────────────────────────────────────┤
  │  Prompt input box                           │
  ├─────────────────────────────────────────────┤
  │  Status bar  [tokens | fill% | elapsed]     │
  └─────────────────────────────────────────────┘
"""

from __future__ import annotations

import difflib
import io
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from rich.console import Console, Group
from rich.layout import Layout
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

# ---------------------------------------------------------------------------
# Message model
# ---------------------------------------------------------------------------


class Role(Enum):
    USER = auto()
    ASSISTANT = auto()
    TOOL = auto()
    DIFF = auto()
    STAGE = auto()  # stage separator used by Standard mode
    PLAN_REVIEW = auto()  # plan-approval prompt (Standard)
    CMD_APPROVAL = auto()  # terminal-command approval prompt


@dataclass
class ChatMessage:
    role: Role
    content: str
    filename: str = ""  # DIFF only
    stage_name: str = ""  # STAGE only
    content_rich: object = None  # TOOL only: pre-styled Rich Text


# ---------------------------------------------------------------------------
# Token tracker
# ---------------------------------------------------------------------------


class TokenTracker:
    """Accumulates token usage across all LLM calls in a session."""

    def __init__(self, context_limit: int = 8192) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.context_limit: int = context_limit

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def fill_fraction(self) -> float:
        if self.context_limit <= 0:
            return 0.0
        return min(1.0, self.total / self.context_limit)

    def update(self, usage: dict) -> None:
        if not usage:
            return
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)


class EnergyTracker:
    """Accumulates energy totals for the TUI status bar."""

    def __init__(self) -> None:
        self.total_joules: float = 0.0
        self.last_display: str = ""
        self.quality: str = "unavailable"
        self.scope: str = "none"
        self.call_count: int = 0

    def update(self, result: object) -> None:
        joules = getattr(result, "energy_joules", None)
        if isinstance(joules, (int, float)):
            self.total_joules += float(joules)
        self.quality = str(getattr(result, "quality", self.quality))
        self.scope = str(getattr(result, "scope", self.scope))
        self.call_count += 1
        try:
            from energy.format import format_joules, format_energy_result

            if self.quality == "approximated":
                self.last_display = f"~{format_joules(self.total_joules)} · approx"
            elif self.scope == "gpu":
                self.last_display = f"{format_joules(self.total_joules)} · GPU"
            elif self.scope == "cpu_package":
                self.last_display = f"{format_joules(self.total_joules)} · CPU"
            else:
                self.last_display = format_energy_result(result)
        except Exception:
            self.last_display = f"{self.total_joules:.1f} J"


# ---------------------------------------------------------------------------
# State object shared between TUI render loop and agent/input threads
# ---------------------------------------------------------------------------


@dataclass
class TUIState:
    messages: list[ChatMessage] = field(default_factory=list)
    current_input: str = ""  # text currently being typed
    thinking: bool = False  # spinner visible when True
    thinking_label: str = "Thinking"
    awaiting_input: bool = False  # True when ready for next prompt
    tokens: TokenTracker = field(default_factory=TokenTracker)
    energy: EnergyTracker = field(default_factory=EnergyTracker)
    start_time: float = field(default_factory=time.monotonic)
    quit: bool = False  # set True to exit the main loop
    chat_title: str = "AutoSE"
    input_line: str = ""
    input_event: threading.Event = field(default_factory=threading.Event)
    generating: bool = False  # True while streaming text from the model
    # Plan-review handshake (Standard mode only)
    plan_review: bool = False
    plan_review_response: str = ""
    plan_review_event: threading.Event = field(default_factory=threading.Event)
    # Terminal-command approval handshake
    cmd_approval: bool = False
    cmd_approval_command: str = ""
    cmd_approval_response: str = ""
    cmd_approval_event: threading.Event = field(default_factory=threading.Event)
    # Scroll state — managed by build_layout
    scroll_offset: int = 0  # lines from the bottom (0 = at bottom)
    _scroll_max: int = 0  # updated each render pass
    _last_total_lines: int = 0
    # Interrupt state
    interrupted: bool = False  # set by Ctrl-C while agent runs; cleared by session loop
    ctrl_c_pending: bool = False  # set on first Ctrl-C while awaiting input
    # Session memory — holds a MemoryManager instance once wired by session.py.
    # Typed as object to avoid importing from logic (which requires sys.path setup).
    memory: object = None
    # Shared core task-session recorder for the currently running prompt.
    task_session: object = None
    last_task_result: object = None

    def add_message(self, role: Role, content: str, **kwargs) -> None:
        self.messages.append(ChatMessage(role=role, content=content, **kwargs))

    def append_to_last(self, text: str) -> None:
        """Append streaming text to the last assistant message."""
        if self.messages and self.messages[-1].role == Role.ASSISTANT:
            self.messages[-1].content += text
        else:
            self.messages.append(ChatMessage(role=Role.ASSISTANT, content=text))

    def elapsed_str(self) -> str:
        seconds = int(time.monotonic() - self.start_time)
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Non-blocking keyboard input (cross-platform)
# Reads one character at a time so we can update current_input while
# rich.Live is rendering.
# ---------------------------------------------------------------------------


def _submit_line(state: TUIState) -> None:
    line = state.current_input
    state.current_input = ""
    state.ctrl_c_pending = False
    if state.cmd_approval:
        state.cmd_approval_response = line
        state.cmd_approval_event.set()
    elif state.plan_review:
        state.plan_review_response = line
        state.plan_review_event.set()
    elif state.awaiting_input:
        state.input_line = line
        state.input_event.set()


def start_keyboard_listener(state: TUIState) -> None:
    """
    Start a persistent background thread to read keyboard input char by char.
    Handles scrolling sequences at any time, and directs text input to the
    currently active prompt (or buffers it if none).
    """

    def _run():
        if sys.platform == "win32":
            import msvcrt

            while not state.quit:
                # msvcrt.getwch() returns immediately when a key is pressed
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):  # Enter
                    _submit_line(state)
                elif ch == "\x03":  # Ctrl-C
                    if state.ctrl_c_pending:
                        # Second Ctrl-C → quit entirely
                        state.quit = True
                        state.ctrl_c_pending = False
                        state.input_event.set()
                        break
                    else:
                        # First Ctrl-C → show hint, wait for second
                        state.ctrl_c_pending = True
                        state.current_input = ""
                elif ch == "\x04":  # Ctrl-D
                    state.quit = True
                    state.ctrl_c_pending = False
                    state.input_event.set()
                    break
                elif ch in ("\x08", "\x7f"):  # Backspace
                    state.ctrl_c_pending = False
                    state.current_input = state.current_input[:-1]
                elif ch == "\x00" or ch == "\xe0":
                    # Special / arrow keys on Windows (legacy console codes)
                    state.ctrl_c_pending = False
                    nch = msvcrt.getwch()
                    if nch == "H":  # Up
                        state.scroll_offset = min(
                            state.scroll_offset + 3, state._scroll_max
                        )
                    elif nch == "P":  # Down
                        state.scroll_offset = max(0, state.scroll_offset - 3)
                    elif nch == "I":  # PgUp
                        state.scroll_offset = min(
                            state.scroll_offset + 20, state._scroll_max
                        )
                    elif nch == "Q":  # PgDn
                        state.scroll_offset = max(0, state.scroll_offset - 20)
                    elif nch == "O":  # End
                        state.scroll_offset = 0
                    # Unknown extended key — consume the second byte and do nothing
                elif ch == "\x1b":
                    # VT escape sequence from Windows Terminal — consume fully so
                    # the individual bytes never reach the printable-character branch.
                    state.ctrl_c_pending = False
                    nch = msvcrt.getwch()
                    if nch == "[":  # CSI sequence
                        # Read parameter/intermediate bytes until the final byte
                        # (ASCII 0x40–0x7E, i.e. @–~).
                        csi_buf = ""
                        csi_final = ""
                        while True:
                            c = msvcrt.getwch()
                            if "\x40" <= c <= "\x7e":
                                csi_final = c
                                break
                            csi_buf += c
                        if csi_final == "A":  # cursor-up / scroll up (VT arrow)
                            state.scroll_offset = min(
                                state.scroll_offset + 3, state._scroll_max
                            )
                        elif csi_final == "B":  # cursor-down / scroll down
                            state.scroll_offset = max(0, state.scroll_offset - 3)
                        elif csi_final == "~":
                            n = int(csi_buf) if csi_buf.isdigit() else 0
                            if n == 5:  # PgUp
                                state.scroll_offset = min(
                                    state.scroll_offset + 20, state._scroll_max
                                )
                            elif n == 6:  # PgDn
                                state.scroll_offset = max(0, state.scroll_offset - 20)
                        elif csi_final == "M" and not csi_buf:
                            # X10 mouse report: 3 raw payload bytes follow
                            msvcrt.getwch()
                            msvcrt.getwch()
                            msvcrt.getwch()
                        # All other CSI sequences (SGR mouse \x1b[<…M,
                        # mode-set \x1b[?…h, etc.) are fully consumed above.
                    # Other ESC introducer bytes (SS2, SS3, OSC…) — intro
                    # byte already read into nch; remaining bytes are part of
                    # a sequence we don't need to act on, so leave them for
                    # the next iteration (they are non-printable in practice).
                elif ch.isprintable():
                    state.ctrl_c_pending = False
                    state.current_input += ch
        else:
            import termios
            import tty
            import traceback

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                # Re-enable output processing (OPOST) so rich.Live can use \\n correctly
                # without resulting in a staircase layout due to missing carriage returns.
                mode = termios.tcgetattr(fd)
                mode[1] |= termios.OPOST
                termios.tcsetattr(fd, termios.TCSADRAIN, mode)
                while not state.quit:
                    ch = sys.stdin.read(1)
                    if not ch:
                        break
                    if ch in ("\r", "\n"):
                        _submit_line(state)
                    elif ch == "\x03":  # Ctrl-C
                        if state.ctrl_c_pending:
                            # Second Ctrl-C → quit entirely
                            state.quit = True
                            state.ctrl_c_pending = False
                            state.input_event.set()
                            break
                        else:
                            # First Ctrl-C → show hint, wait for second
                            state.ctrl_c_pending = True
                            state.current_input = ""
                    elif ch == "\x04":  # Ctrl-D
                        state.quit = True
                        state.ctrl_c_pending = False
                        state.input_event.set()
                        break
                    elif ch in ("\x08", "\x7f"):  # Backspace
                        state.ctrl_c_pending = False
                        state.current_input = state.current_input[:-1]
                    elif ch == "\x1b":  # Escape sequence
                        state.ctrl_c_pending = False
                        seq = sys.stdin.read(1)
                        if seq == "[":  # CSI sequence
                            # Read parameter/intermediate bytes until the final byte
                            # (ASCII 0x40–0x7E, i.e. @–~).
                            csi_buf = ""
                            csi_final = ""
                            while True:
                                c = sys.stdin.read(1)
                                if "\x40" <= c <= "\x7e":
                                    csi_final = c
                                    break
                                csi_buf += c
                            if csi_final == "A":  # Up
                                state.scroll_offset = min(
                                    state.scroll_offset + 3, state._scroll_max
                                )
                            elif csi_final == "B":  # Down
                                state.scroll_offset = max(0, state.scroll_offset - 3)
                            elif csi_final == "~":
                                n = int(csi_buf) if csi_buf.isdigit() else 0
                                if n == 5:  # PgUp
                                    state.scroll_offset = min(
                                        state.scroll_offset + 20, state._scroll_max
                                    )
                                elif n == 6:  # PgDn
                                    state.scroll_offset = max(
                                        0, state.scroll_offset - 20
                                    )
                            elif csi_final == "F":  # End
                                state.scroll_offset = 0
                            elif csi_final == "M" and not csi_buf:
                                # X10 mouse report: 3 raw payload bytes follow
                                sys.stdin.read(1)
                                sys.stdin.read(1)
                                sys.stdin.read(1)
                            # All other CSI sequences (SGR mouse \x1b[<…M/m,
                            # mode-set \x1b[?…h, focus events, etc.) fully
                            # consumed by the loop above.
                    elif ch.isprintable():
                        state.ctrl_c_pending = False
                        state.current_input += ch
            except Exception as e:
                with open("keyboard_thread_error.log", "a") as f:
                    f.write(traceback.format_exc() + "\n")
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_messages_to_lines(messages: list[ChatMessage], width: int) -> list[str]:
    """Render all messages into a flat list of ANSI-escaped lines."""
    buf = io.StringIO()
    tmp = Console(file=buf, width=width, force_terminal=True, highlight=False)
    tmp.print(_render_messages(messages), end="")
    return buf.getvalue().split("\n")


def _render_diff(old_text: str, new_text: str, filename: str) -> Text:
    if not old_text.strip():
        new_lines = len(new_text.splitlines())
        return Text(f"Created file {filename} ({new_lines} lines)", style="green")

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    additions = sum(
        1 for line in difflib.ndiff(old_lines, new_lines) if line.startswith("+ ")
    )
    deletions = sum(
        1 for line in difflib.ndiff(old_lines, new_lines) if line.startswith("- ")
    )

    return Text(
        f"Updated file {filename} (+{additions} / -{deletions} lines)", style="yellow"
    )


def _role_label(role: Role) -> Text:
    styles = {
        Role.USER: ("You", "bold cyan"),
        Role.ASSISTANT: ("AutoSE", "bold green"),
        Role.TOOL: ("Tool", "dim yellow"),
    }
    label, style = styles.get(role, ("?", "white"))
    return Text(label, style=style)


def _render_messages(messages: list[ChatMessage]) -> Group:
    renderables = []
    for msg in messages:
        if msg.role == Role.STAGE:
            renderables.append(
                Rule(f"[bold magenta]{msg.stage_name}[/bold magenta]", style="magenta")
            )
        elif msg.role == Role.PLAN_REVIEW:
            renderables.append(
                Panel(
                    Text.from_markup(
                        "[bold]Press Enter, or type [cyan]A[/cyan] (or [cyan]approve[/cyan]) to proceed  "
                        "·  or describe the changes you want"
                    ),
                    title="[bold cyan]Plan Review[/bold cyan]",
                    border_style="cyan",
                    padding=(0, 2),
                )
            )
        elif msg.role == Role.CMD_APPROVAL:
            renderables.append(
                Panel(
                    Text.assemble(
                        Text("$ ", style="bold green"),
                        Text(msg.content, style="bold white"),
                        Text("\n\nPress "),
                        Text("Enter", style="bold cyan"),
                        Text(" or "),
                        Text("Y", style="bold cyan"),
                        Text(" to allow  ·  "),
                        Text("N", style="bold red"),
                        Text(" to deny"),
                    ),
                    title="[bold yellow]⚙ Terminal Command Approval[/bold yellow]",
                    border_style="yellow",
                    padding=(0, 2),
                )
            )
        elif msg.role == Role.DIFF:
            parts = msg.content.split("\x00", 1)
            old_text = parts[0] if len(parts) == 2 else ""
            new_text = parts[1] if len(parts) == 2 else parts[0]
            summary_text = _render_diff(old_text, new_text, msg.filename)
            renderables.append(
                Panel(
                    summary_text,
                    title=f"[yellow]File Update[/yellow]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
        elif msg.role == Role.TOOL:
            if msg.content_rich is not None:
                renderables.append(msg.content_rich)
            else:
                renderables.append(
                    Text.assemble(
                        Text("⟳ ", style="dim"), Text(msg.content, style="dim")
                    )
                )
        else:
            label = _role_label(msg.role)
            if msg.role == Role.ASSISTANT:
                body = Markdown(msg.content or " ")
            else:
                body = Text(msg.content, style="white")
            renderables.append(Text.assemble(label, Text("  ", style="dim")))
            renderables.append(body)
            renderables.append(Text(""))
    return Group(*renderables)


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _build_status_bar(state: TUIState) -> Panel:
    tokens = state.tokens
    pct = tokens.fill_fraction * 100

    if pct >= 90:
        bar_style, pct_style = "bold red", "red"
    elif pct >= 70:
        bar_style, pct_style = "yellow", "yellow"
    else:
        bar_style, pct_style = "green", "green"

    filled = int(tokens.fill_fraction * 20)
    bar_chars = "█" * filled + "░" * (20 - filled)

    token_text = Text(f"Tokens: {tokens.total:,}", style="white")
    bar_text = Text(f"[{bar_chars}]", style=bar_style)
    pct_text = Text(f"{pct:.1f}% context", style=pct_style)
    energy_label = state.energy.last_display or "Energy: —"
    energy_text = Text(energy_label if energy_label.startswith("Energy") else f"Energy: {energy_label}", style="bright_magenta")
    elapsed_text = Text(f"Elapsed: {state.elapsed_str()}", style="bright_blue")

    frame = _SPINNER_FRAMES[int(time.monotonic() * 10) % len(_SPINNER_FRAMES)]

    if state.cmd_approval:
        status_indicator = Text("◆ awaiting terminal approval", style="bold yellow")
    elif state.plan_review:
        status_indicator = Text("◆ awaiting plan approval", style="bold cyan")
    elif state.thinking:
        status_indicator = Text.from_markup(
            f"[bold yellow]{frame} {state.thinking_label}...[/bold yellow]"
        )
    elif state.generating:
        status_indicator = Text.from_markup(
            f"[bold green]{frame} Generating...[/bold green]"
        )
    elif state.awaiting_input:
        status_indicator = Text("● ready", style="bold cyan")
    else:
        status_indicator = Text("◌ standby", style="dim")

    status_line = Text.assemble(
        token_text,
        Text("   "),
        bar_text,
        Text("  "),
        pct_text,
        Text("   "),
        energy_text,
        Text("   "),
        elapsed_text,
        Text("   "),
        status_indicator,
    )
    return Panel(status_line, style="dim", padding=(0, 1), height=3)


# ---------------------------------------------------------------------------
# Full layout builder
# ---------------------------------------------------------------------------


def build_layout(state: TUIState) -> Layout:
    try:
        term_cols = os.get_terminal_size().columns
        term_rows = os.get_terminal_size().lines
    except OSError:
        term_cols, term_rows = 80, 24

    layout = Layout()
    layout.split_column(
        Layout(name="chat", ratio=1),
        Layout(name="input", size=3),
        Layout(name="status", size=3),
    )

    # Inner dimensions: panel border = 2 rows / 2 cols; padding=(0,1) = 2 more cols
    chat_inner_h = max(4, term_rows - 8)
    chat_inner_w = max(20, term_cols - 4)

    lines = _render_messages_to_lines(state.messages, chat_inner_w)
    total = len(lines)

    added = total - state._last_total_lines
    state._last_total_lines = total

    state._scroll_max = max(0, total - chat_inner_h)

    if state.scroll_offset > 0:
        state.scroll_offset += added

    state.scroll_offset = max(0, min(state.scroll_offset, state._scroll_max))

    end = total - state.scroll_offset
    start = max(0, end - chat_inner_h)
    visible = lines[start:end]
    while len(visible) < chat_inner_h:
        visible.append("")

    chat_content = Text.from_ansi("\n".join(visible))

    if state.scroll_offset > 0:
        scroll_info = "  [dim]\u2191 scrolled · \u2193/End = bottom[/dim]"
    elif state._scroll_max > 0:
        scroll_info = "  [dim]\u2191\u2193 scroll[/dim]"
    else:
        scroll_info = ""

    layout["chat"].update(
        Panel(
            chat_content,
            title=f"[bold]{state.chat_title}[/bold]{scroll_info}",
            border_style="bright_blue",
            padding=(0, 1),
        )
    )

    # Show blinking cursor only while awaiting input
    cursor = "█" if state.awaiting_input else ""
    if state.cmd_approval:
        input_text = Text.from_markup(
            f"[bold yellow]›[/bold yellow] [dim italic]Y=allow · N=deny[/dim italic]   "
            f"{state.current_input}{cursor}"
        )
        input_border = "yellow"
    elif state.plan_review:
        hint = "[dim italic]A=approve · or describe changes[/dim italic]   "
        input_text = Text.from_markup(
            f"[bold cyan]›[/bold cyan] [dim italic]A=approve · or describe changes[/dim italic]   "
            f"{state.current_input}{cursor}"
        )
        input_border = "bright_cyan"
    else:
        if state.ctrl_c_pending:
            hint = "[bold red]  Ctrl-C again to quit[/bold red]"
        elif not state.awaiting_input:
            hint = "[dim]  (Ctrl-C to quit)[/dim]"
        else:
            hint = ""
        input_text = Text.from_markup(
            f"[bold cyan]>[/bold cyan] {state.current_input}{cursor}{hint}"
        )
        input_border = "cyan"
    layout["input"].update(Panel(input_text, border_style=input_border, padding=(0, 1)))

    layout["status"].update(_build_status_bar(state))

    return layout


# ---------------------------------------------------------------------------
# Public helper: patch an agent's _execute_tool to emit TOOL messages
# ---------------------------------------------------------------------------

_TOOL_LABELS: dict[str, tuple[str, str]] = {
    "read_file": ("📖", "Read"),
    "write_file": ("✏️ ", "Write"),
    "edit_file": ("✏️ ", "Edit"),
    "list_files": ("📂", "List"),
    "search_files": ("🔍", "Search"),
    "find_files": ("🔎", "Find"),
    "run_command": ("⚙️ ", "Run"),
    "run_tests": ("🧪", "Run tests"),
}


def _tool_friendly_label(name: str, args: dict) -> Text:
    entry = _TOOL_LABELS.get(name)
    if entry is None:
        plain = name.replace("_", " ").title()
        return Text.assemble(Text("⟳ ", style="dim"), Text(plain, style="dim yellow"))

    icon, verb = entry

    if name in ("read_file", "write_file", "edit_file"):
        raw = args.get("path", "")
        target = Path(raw).name if raw else ""
        target_style = "bold cyan"
    elif name == "list_files":
        raw = args.get("directory", "") or args.get("path", "")
        target = Path(raw).name or raw or "."
        target_style = "bold blue"
    elif name == "search_files":
        target = f'"{args.get("text", "")}"'
        target_style = "italic white"
    elif name == "find_files":
        target = args.get("name_pattern", "")
        target_style = "bold cyan"
    elif name in ("run_command", "run_tests"):
        cmd = args.get("command", "")
        target = (cmd[:40] + "…") if len(cmd) > 40 else cmd
        target_style = "bold"
    else:
        target = ""
        target_style = ""

    parts: list = [Text(f"{icon} ", style="dim"), Text(verb, style="yellow")]
    if target:
        parts.append(Text(" "))
        parts.append(Text(target, style=target_style))
    return Text.assemble(*parts)


def patch_execute_tool(agent: object, state: "TUIState") -> None:
    """Wrap agent._execute_tool to emit a TOOL message and gate run_command on approval."""
    import json as _json
    from core.tui_bridge import emit_event

    original = agent._execute_tool  # type: ignore[attr-defined]

    def tracked(tool_call: dict) -> str:
        name = tool_call["function"]["name"]
        try:
            args = _json.loads(tool_call["function"].get("arguments", "{}"))
        except Exception:
            args = {}

        if name == "run_command" and "run_command" in getattr(agent, "_tools", {}):
            command = args.get("command", "")
            emit_event(state, "approval_requested", tool=name, command=command)
            state.cmd_approval_command = command
            state.cmd_approval_response = ""
            state.cmd_approval_event.clear()
            state.messages.append(ChatMessage(role=Role.CMD_APPROVAL, content=command))
            state.cmd_approval = True
            state.cmd_approval_event.wait()
            state.cmd_approval = False
            allowed = state.cmd_approval_response.strip().lower() in ("y", "yes", "")
            emit_event(
                state,
                "approval_resolved",
                tool=name,
                command=command,
                allowed=allowed,
            )
            if not allowed:
                return "Command was denied by the user. Do not retry this command."
            # Fall through to execute

        rich_label = _tool_friendly_label(name, args)
        emit_event(state, "tool_called", tool=name, arguments=args)
        state.messages.append(
            ChatMessage(
                role=Role.TOOL, content=rich_label.plain, content_rich=rich_label
            )
        )
        return original(tool_call)

    agent._execute_tool = tracked  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Public helper: make a diff ChatMessage
# ---------------------------------------------------------------------------


def make_diff_message(old_text: str, new_text: str, filename: str) -> ChatMessage:
    packed = f"{old_text}\x00{new_text}"
    return ChatMessage(role=Role.DIFF, content=packed, filename=filename)
