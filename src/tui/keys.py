"""Reading keypresses while the interface is busy.

A slash command can only be typed *between* turns, which makes it useless for
anything you want to change while the model is working — expanding the
thinking block being exactly that. This module puts the terminal into cbreak
mode for the duration of the live indicator so single keys can be read without
waiting for Enter, and restores it the moment the indicator stops.

Two properties matter and are load-bearing:

* **cbreak, never raw.** ``tty.setraw`` also clears ``ISIG``, which would stop
  ctrl-c from interrupting a run — trading a far more important key for a
  convenience one. ``setcbreak`` keeps signals working.
* **Nothing typed is thrown away.** Keys that are not shortcuts are kept, and
  the Adapter hands them to the next prompt, so typing ahead during a long turn
  behaves the way it does in a shell rather than vanishing into the void.

On a platform or a stream where none of this is possible (Windows, a pipe, a
CI log) every method is a no-op and :attr:`KeyReader.active` stays ``False`` —
callers are expected to ask, and to offer the slash command instead.
"""

from __future__ import annotations

import atexit
import os
import re
import sys

__all__ = ["KeyReader", "CTRL_O", "strip_escapes"]

try:  # pragma: no cover - the import itself is the platform test
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows has no termios
    select = termios = tty = None  # type: ignore[assignment]

#: The shortcut the interface offers. A control character on purpose: a plain
#: letter would be swallowed out of anything the user types ahead.
CTRL_O = "\x0f"

#: Arrow keys and the like arrive as escape sequences; they are not text, and
#: carrying them into the next prompt would paste garbage into it.
_ESCAPE_SEQUENCE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI: arrows, home/end, mouse reports
    r"|\x1bO[@-~]"  # SS3: the function keys some terminals send
    r"|\x1b[@-Z\\-_]?"  # anything else introduced by escape, and a lone one
)


def strip_escapes(text: str) -> str:
    """Drop terminal escape sequences, keeping only what was really typed."""
    return _ESCAPE_SEQUENCE.sub("", text)


class KeyReader:
    """Non-blocking single-keypress reads, for as long as it is started."""

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self._saved: list | None = None
        # Restoring a terminal is not optional: an interpreter that exits with
        # the tty still in cbreak leaves the user's shell without an echo.
        atexit.register(self.stop)

    @property
    def available(self) -> bool:
        """Whether this stream can be read a key at a time at all."""
        if termios is None:
            return False
        try:
            return bool(self._stream is not None and self._stream.isatty())
        except (AttributeError, ValueError):
            return False

    @property
    def active(self) -> bool:
        """Whether keys are being captured right now."""
        return self._saved is not None

    def start(self) -> None:
        if self.active or not self.available:
            return
        fd = self._fd()
        if fd is None:
            return
        try:
            saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except termios.error:  # pragma: no cover - an unusual tty
            return
        self._saved = saved

    def stop(self) -> None:
        if self._saved is None:
            return
        saved, self._saved = self._saved, None
        fd = self._fd()
        if fd is None:
            return
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except termios.error:  # pragma: no cover - an unusual tty
            return

    def read(self) -> str:
        """Every key waiting right now, or ``""``. Never blocks."""
        if not self.active:
            return ""
        fd = self._fd()
        if fd is None:
            return ""
        chunks: list[str] = []
        try:
            while select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 1024)
                if not data:
                    break
                chunks.append(data.decode("utf-8", "ignore"))
        except OSError:  # pragma: no cover - the stream went away mid-turn
            return "".join(chunks)
        return "".join(chunks)

    def _fd(self) -> int | None:
        try:
            return self._stream.fileno()
        except (AttributeError, ValueError, OSError):
            return None
