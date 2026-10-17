"""Turning ctrl-c into a graceful stop, and a second one into an abort (DR-030).

DR-030 removed the round and token caps that used to end a long run on their
own, which makes ctrl-c load-bearing rather than an escape hatch: it is now the
normal way a user says "that's enough". Killing the turn outright is the wrong
response to that, because it throws away the draft the model has written, the
artifacts the finished stages produced, and the summary of what was already
changed on disk — exactly what someone stopping a long run wants to see.

So the first ctrl-c asks the session to stop (``Session.request_stop``) and
lets the turn unwind through :class:`logic.errors.RunCancelled`, which reports
a partial outcome the same way any other stop reason does. A second ctrl-c is
read as "I meant now": the default ``KeyboardInterrupt`` is restored and
raised, and the caller's existing handler deals with it.

Both terminal Adapters use this — the interactive app and the JSON one — which
is the only reason it is a module rather than eight lines inside `app.py`.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = ["stop_on_interrupt"]


@contextmanager
def stop_on_interrupt(
    request_stop: Callable[[], bool],
    *,
    announce: Callable[[bool], None] | None = None,
) -> Iterator[None]:
    """Install the two-stage ctrl-c handler for the duration of the block.

    *request_stop* is called on the first ctrl-c — ``Session.request_stop``,
    which returns whether it was the first such request. *announce* is then
    called with that answer, so an Adapter can tell the user what will happen
    ("stopping after this step; ctrl-c again to abort") instead of leaving a
    keypress looking ignored.

    A no-op where signal handling is unavailable — off the main thread, or on a
    platform that refuses the handler — rather than a failure: the caller's
    plain ``KeyboardInterrupt`` path is still there underneath.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    asked = False

    def handler(signum: int, frame) -> None:
        nonlocal asked
        if asked:
            # Second press: restore the default so even this handler cannot
            # swallow a third one, then let the exception fly.
            signal.signal(signal.SIGINT, previous)
            raise KeyboardInterrupt
        asked = True
        first = request_stop()
        if announce is not None:
            announce(first)

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # pragma: no cover - platform dependent
        yield
        return

    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
