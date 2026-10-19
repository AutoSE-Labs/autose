"""Live in-flight feedback: thinking, tool calls, elapsed time.

An event sink that renders what is happening *while* it happens. On the local
hardware AutoSE targets, a turn can run for a minute before the first
character of the answer exists (DR-015, DR-016) — with nothing on screen, that
is indistinguishable from a hang. This is the module that makes the wait
legible.

Reasoning gets a *block* rather than a single self-erasing line. A chain of
thought scrolling through 72 characters cannot be read at the speed it is
produced, so what the user actually got from it was motion, not information.
The block keeps the last few wrapped lines on screen, and every thought is
retained for the whole turn so ``/thinking`` can print it in full afterwards.

The block is resized with **ctrl-o**, read live off the terminal, because the
moment you want more of a thought is while it is still streaming — and a slash
command cannot be typed until the turn is over, by which point the question has
answered itself.

It is presentation only: it reacts to events `logic/` emits and never asks it
for anything.
"""

from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from logic.common.events import Event

from .keys import CTRL_O, KeyReader, strip_escapes
from .render import (
    classification_line,
    denied_line,
    loopback_notice,
    queued_input_line,
    requirements_table,
    stage_header,
    thinking_block,
    thinking_hidden_line,
    thinking_summary,
    verdict_panel,
)
from .theme import THEME

__all__ = ["ActivityIndicator"]

#: Characters of one-line detail (a tool's argument) shown beside the state.
_DETAIL_TAIL = 72

#: Wrapped lines of thought kept on screen while it streams. Three is enough
#: to read a sentence as it forms without the block dominating the terminal.
_COLLAPSED_LINES = 3

#: Lines shown once the user has asked for more (ctrl-o, or ``/thinking``).
_EXPANDED_LINES = 16

#: How much type-ahead is carried into the next prompt. A bound, not a limit
#: anyone will meet: past this the user is not typing a request into the dark.
_MAX_TYPED = 4096


class _TickingStatus:
    """Recomputes the status line on every automatic Rich repaint.

    ``Live`` repaints roughly every ``1/refresh_per_second`` seconds on its
    own — that is what animates the spinner glyph even during a call that
    never touches this class again. A bare ``Spinner`` only changes its
    *text* when explicitly told to, though, so a long blocking call with no
    intermediate events (a classification call, a non-streaming round) left
    the elapsed-seconds figure frozen at whatever it was on the last event.
    Recomputing the frame inside ``__rich_console__`` — invoked on every
    automatic repaint — keeps the clock ticking with no event required.
    """

    def __init__(self, indicator: ActivityIndicator, spinner: Spinner) -> None:
        self._indicator = indicator
        self._spinner = spinner

    def __rich_console__(self, console, options):
        # SLF001: this renderable and `ActivityIndicator` are one component
        # split across two classes only because Rich needs a separate object
        # to call back into. The "private" members are its own.
        self._indicator.pump_keys()
        self._spinner.text = self._indicator._frame()  # noqa: SLF001
        yield self._spinner
        block = self._indicator._thought_block(  # noqa: SLF001
            options.max_width, console.size.height
        )
        if block is not None:
            yield block
        queued = self._indicator._queued_input()  # noqa: SLF001
        if queued is not None:
            yield queued


class ActivityIndicator:
    """Renders a live status line, and prints tool activity as it resolves."""

    def __init__(self, console: Console, keys: KeyReader | None = None) -> None:
        self._console = console
        self._keys = keys if keys is not None else KeyReader()
        self._typed = ""
        self._live: Live | None = None
        self._spinner = Spinner("dots", style=THEME.accent)
        self._turn_started = 0.0
        self._state = "thinking"
        self._detail = ""
        self._thought = ""
        self._thought_label = "thinking"
        self._thought_started = 0.0
        self._segments: list[tuple[str, str]] = []
        self._answering = False
        # A live-updating spinner is meaningful in a terminal and pure noise in
        # a pipe or a CI log, where every redraw becomes another line. The test
        # is the actual tty, not `console.is_terminal`: FORCE_COLOR makes Rich
        # report a terminal so that colour survives a pipe, which says nothing
        # about whether anything can be redrawn in place.
        self._animate = console.is_terminal and _is_tty(console)
        self._stages: tuple[str, ...] = ()
        #: Whether this turn ran a multi-stage workflow, which changes what the
        #: app renders at the end.
        self.staged = False
        #: User preferences, driven by ``/thinking``.
        self.show_thinking = True
        self.expanded = False

    # -- lifecycle ------------------------------------------------------

    def start(self, label: str = "thinking") -> None:
        """Begin a turn: reset the clock and show the indicator."""
        self._turn_started = time.monotonic()
        self._answering = False
        self._thought = ""
        self._detail = ""
        self._segments = []
        self._stages = ()
        self.staged = False
        self._show(label)

    def stop(self) -> None:
        # The reader is released first: an approval prompt, or a traceback, must
        # never find the terminal still in cbreak.
        self._keys.stop()
        if self._live is not None:
            self._live.stop()
            self._live = None

    def begin_answer(self) -> None:
        """Called when the first answer token arrives: the wait is over."""
        if not self._answering:
            self._answering = True
            self._flush_thought()
            self.stop()

    def finish_turn(self) -> None:
        """End of turn: keep any thought that never got flushed."""
        self._flush_thought()

    @property
    def thoughts(self) -> tuple[tuple[str, str], ...]:
        """Every thought from the current turn — what ``/thinking`` prints."""
        pending = (
            [(self._thought_label, self._thought)] if self._thought.strip() else []
        )
        return tuple(self._segments + pending)

    @property
    def elapsed(self) -> float:
        """Seconds since the *turn* began — not since the indicator restarted.

        Approvals stop and restart the indicator mid-turn; the number the user
        reads at the end has to be the whole turn, or it silently under-reports
        the wait by however long they took to answer.
        """
        return time.monotonic() - self._turn_started if self._turn_started else 0.0

    def _show(self, label: str) -> None:
        self._state = label
        if not self._animate or self._live is not None:
            return
        self._spinner.update(text=self._frame())
        self._live = Live(
            _TickingStatus(self, self._spinner),
            console=self._console,
            refresh_per_second=8,
            transient=True,
        )
        self._live.start()
        self._keys.start()

    # -- the event sink -------------------------------------------------

    def emit(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    # -- classification and stages ---------------------------------------

    def _on_classification_started(self, event: Event) -> None:
        self._show("choosing an approach")
        self._state = "choosing an approach"
        self._refresh()

    def _on_classification_finished(self, event: Event) -> None:
        # Printed as it happens: the user should see which pipeline is about to
        # run before it starts, not after its first stage has already gone by.
        self._print_above(
            classification_line(
                event.data.get("tier", ""),
                event.data.get("source", "model"),
                (
                    event.data.get("effective_tier", "")
                    if event.data.get("ratcheted")
                    else ""
                ),
            )
        )

    def _on_workflow_started(self, event: Event) -> None:
        self.staged = True
        self._stages = tuple(event.data.get("stages") or ())
        self._print_above(
            Text(
                f"  {event.data.get('tier', '')}: {' → '.join(self._stages)}",
                style=THEME.muted,
            )
        )

    def _on_stage_started(self, event: Event) -> None:
        stage = event.data.get("stage", "")
        position = self._stages.index(stage) + 1 if stage in self._stages else 0
        self._flush_thought()
        self._print_above(
            stage_header(
                stage,
                event.data.get("title", stage),
                event.data.get("iteration", 1),
                total_stages=len(self._stages),
                position=position,
            )
        )
        self._state = event.message or stage
        self._detail = ""
        self._refresh()

    def _on_stage_output_delta(self, event: Event) -> None:
        # A stage's own prose, so a long stage shows progress without the
        # transcript filling with a draft that is about to be replaced by its
        # parsed artifact.
        self._append_thought(event.message, label=f"{self._state} — draft")

    def _on_stage_finished(self, event: Event) -> None:
        self._flush_thought()
        requirements = event.data.get("requirements") or []
        if requirements:
            self._print_above(requirements_table(requirements))
        self._state = "thinking"
        self._refresh()

    def _on_stage_retry(self, event: Event) -> None:
        self._print_above(
            Text(
                f"  {THEME.warn_glyph} rejected: {event.data.get('reason', '')}",
                style=THEME.warning,
            )
        )

    def _on_validation_verdict(self, event: Event) -> None:
        self._print_above(verdict_panel(dict(event.data)))

    def _on_loopback(self, event: Event) -> None:
        self._print_above(loopback_notice(dict(event.data)))

    def _on_agent_reasoning_delta(self, event: Event) -> None:
        self._state = "thinking"
        self._append_thought(event.message)

    def _on_agent_reasoning(self, event: Event) -> None:
        # A whole chain of thought at once (a non-streaming round): keep it,
        # then let the same flush path summarise it.
        self._append_thought(event.message)

    def _on_approval_requested(self, event: Event) -> None:
        # The gate is about to take over the terminal; get out of its way, and
        # keep whatever it was thinking rather than letting the prompt eat it.
        self._flush_thought()
        self.stop()

    def _on_approval_resolved(self, event: Event) -> None:
        if not event.data.get("approved"):
            self._console.print(denied_line(str(event.data.get("reason", ""))))
        self._resume("working")

    def _on_tool_called(self, event: Event) -> None:
        self._state = f"{event.data.get('tool', 'tool')}"
        self._detail = _describe_arguments(event.data.get("arguments"))
        self._refresh()

    def _on_tool_completed(self, event: Event) -> None:
        tool = event.data.get("tool", "tool")
        chars = event.data.get("result_chars", 0)
        line = Text()
        line.append(f"  {THEME.tool_glyph} ", style=THEME.tool)
        line.append(tool, style=THEME.tool)
        detail = f"  {chars:,} chars"
        if event.data.get("artifact"):
            detail += f" → {event.data['artifact']}"
        elif event.data.get("truncated"):
            detail += " (truncated)"
        line.append(detail, style=THEME.muted)
        self._print_above(line)
        self._state = "thinking"
        self._detail = ""
        self._refresh()

    def _on_agent_cancelled(self, event: Event) -> None:
        # DR-030: the stop the user asked for actually landed. Worth a line of
        # its own — between asking and the turn unwinding there can be a whole
        # round of inference, and silence there reads as a keypress ignored.
        rounds = event.data.get("rounds", 0)
        self._print_above(
            Text(
                f"  {THEME.warn_glyph} {event.data.get('stage', 'agent')} stopped "
                f"after {rounds} round(s) — keeping what it had",
                style=THEME.warning,
            )
        )

    def _on_agent_round_cap_reached(self, event: Event) -> None:
        self._print_above(
            Text(
                f"  {THEME.warn_glyph} stopped after "
                f"{event.data.get('max_tool_rounds')} tool rounds",
                style=THEME.warning,
            )
        )

    def _on_tool_call_repeated(self, event: Event) -> None:
        # The refusals are the visible run-up to a stall. Without them the run
        # ends on a verdict with nothing behind it, and the user is told the
        # model went in circles with no way to see it happen.
        self._print_above(
            Text(
                f"  {THEME.warn_glyph} {event.message}",
                style=THEME.warning,
            )
        )

    def _on_agent_stalled(self, event: Event) -> None:
        self._print_above(Text(f"  {THEME.warn_glyph} {event.message}", style=THEME.warning))

    def _on_agent_context_thrashing(self, event: Event) -> None:
        self._print_above(Text(f"  {THEME.warn_glyph} {event.message}", style=THEME.warning))

    def _on_context_window_changed(self, event: Event) -> None:
        # DR-059: a window matched to the loaded model changes without anyone
        # asking, so say so once; a window the user picked needs no echo.
        reason = event.data.get("reason")
        tokens = event.data.get("tokens")
        if not reason or not isinstance(tokens, int):
            return
        size = f"{tokens // 1024}k" if tokens % 1024 == 0 else f"{tokens:,}"
        self._print_above(
            Text(f"  context window {size} tokens — {reason}", style=THEME.muted)
        )

    def _on_agent_nudged(self, event: Event) -> None:
        # DR-019: the model went quiet rather than answering; without this,
        # the nudge (and its bounded retry) is invisible and looks like a hang.
        self._print_above(
            Text(
                f"  {THEME.warn_glyph} model went quiet — {event.message} "
                f"(attempt {event.data.get('attempt')})",
                style=THEME.warning,
            )
        )
        self._state = "thinking"
        self._refresh()

    # -- keys ---------------------------------------------------------------

    def pump_keys(self) -> None:
        """Act on anything pressed since the last repaint.

        Called from the repaint itself, which Rich runs eight times a second —
        so a shortcut lands within ~125ms with no thread of our own, and with
        no polling at all when the indicator is not showing.
        """
        keys = self._keys.read()
        if not keys:
            return
        for char in strip_escapes(keys):
            if char == CTRL_O:
                self.cycle_thinking()
            elif char in ("\x7f", "\b"):
                # Typing ahead is typing blind; let backspace still work.
                self._typed = self._typed[:-1]
            elif char in ("\r", "\n"):
                # A blind Enter must not submit: the user cannot see what they
                # typed, so it is carried to the prompt to be confirmed there.
                continue
            elif char.isprintable() and len(self._typed) < _MAX_TYPED:
                self._typed += char

    def cycle_thinking(self) -> None:
        """collapsed → expanded → hidden → collapsed."""
        if not self.show_thinking:
            self.show_thinking, self.expanded = True, False
        elif not self.expanded:
            self.expanded = True
        else:
            self.show_thinking, self.expanded = False, False
        self._refresh()

    def take_typed(self) -> str:
        """Hand over anything typed during the turn, and forget it."""
        typed, self._typed = self._typed, ""
        return typed.strip()

    @property
    def keys_captured(self) -> bool:
        """Whether shortcuts can be offered — or only the slash command."""
        return self._keys.active

    # -- thought bookkeeping ----------------------------------------------

    def _append_thought(self, text: str, *, label: str = "thinking") -> None:
        if not text:
            return
        if not self._thought:
            self._thought_started = time.monotonic()
            self._thought_label = label
        self._thought += text
        self._refresh()

    def _flush_thought(self) -> None:
        """Retire the current thought: keep it, and leave one line behind.

        The live block is transient by design — it disappears when the wait
        ends. What stays in the transcript is a single line saying a thought
        happened, how long it took, and how to read it.
        """
        thought = self._thought
        self._thought = ""
        if not thought.strip():
            return
        self._segments.append((self._thought_label, thought))
        elapsed = (
            time.monotonic() - self._thought_started if self._thought_started else 0.0
        )
        # "thought for 12s" reads as a report; "thinking for 12s" reads as a
        # status line that failed to clear.
        label = "thought" if self._thought_label == "thinking" else self._thought_label
        self._print_above(thinking_summary(len(thought), elapsed, label=label))

    # -- rendering -------------------------------------------------------

    def _frame(self) -> Text:
        # no_wrap + ellipsis keeps the indicator to exactly one row whatever
        # the terminal width, so it overwrites itself cleanly.
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f" {self._state}", style=THEME.accent)
        line.append(f"  {self.elapsed:.0f}s", style=THEME.muted)
        if self._detail:
            line.append(f"  {self._detail}", style=THEME.thinking)
        return line

    def _thought_block(self, width: int, height: int = 0):
        """The block under the status line, or ``None`` when there is none."""
        if not self._thought.strip():
            return None
        if not self.show_thinking:
            # Hidden, but not gone: without this line there is nothing on
            # screen to say a key would bring it back.
            return (
                thinking_hidden_line(self._hint())
                if self.keys_captured
                else None
            )
        wanted = _EXPANDED_LINES if self.expanded else _COLLAPSED_LINES
        if height:
            # Never ask Live for more rows than the terminal has: it would
            # scroll the block instead of redrawing it in place.
            wanted = max(1, min(wanted, height - 4))
        return thinking_block(
            self._thought,
            width=width,
            max_lines=wanted,
            expanded=self.expanded,
            label=self._thought_label,
            hint=self._hint(),
        )

    def _queued_input(self):
        """The type-ahead buffer, echoed back because the terminal will not."""
        return queued_input_line(self._typed, THEME.continuation) if self._typed else None

    def _hint(self) -> str:
        """What to tell the user they can do about the block, right now."""
        if not self.keys_captured:
            # No key can be read here, so promising one would be a lie.
            return "/thinking to read it all"
        if not self.show_thinking:
            return "ctrl-o to show"
        return "ctrl-o to collapse" if self.expanded else "ctrl-o to expand"

    def _refresh(self) -> None:
        # State/thought changes still get an immediate repaint rather than
        # waiting for the next automatic tick; the tick itself now comes from
        # _TickingStatus regardless of whether this is ever called.
        if self._live is None or not self._animate:
            return
        self._spinner.update(text=self._frame())
        self._live.refresh()

    def note(self, message: str, *, style: str | None = None) -> None:
        """Print one advisory line above the live status.

        Public because an Adapter has things to say that no event covers —
        DR-030's "ctrl-c again to abort" being the case in point, since a
        keypress is not something ``logic`` emits.
        """
        self._print_above(Text(f"  {message}", style=style or THEME.muted))

    def _print_above(self, renderable) -> None:
        """Emit a permanent line without disturbing the live status."""
        if self._live is not None:
            self._live.console.print(renderable)
        else:
            self._console.print(renderable)

    def _resume(self, label: str) -> None:
        """Restart the indicator after an approval prompt took the terminal."""
        if not self._answering:
            self._show(label)


def _is_tty(console: Console) -> bool:
    """Whether the console's underlying stream can actually be redrawn."""
    stream = getattr(console, "file", None)
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):
        # A closed or unusual stream is not something to animate into.
        return False


def _describe_arguments(arguments: object) -> str:
    """The one argument worth showing on a status line."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("path", "command", "url", "query", "text", "name_pattern", "directory"):
        value = arguments.get(key)
        if value:
            return " ".join(str(value).split())[:_DETAIL_TAIL]
    return ""
