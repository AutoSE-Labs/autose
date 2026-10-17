"""The cooperative stop signal every long-running loop watches (DR-030).

DR-030 removes the caps that used to guarantee a run terminated on its own, so
something has to take their place: the person driving it. That is this object.
An Adapter hands one :class:`CancelToken` to the session; the agent loop and
the staged runner check it at the points where stopping is cheap and the work
so far is still worth keeping — between rounds, between tool calls, between
stages — and unwind with a partial result rather than throwing the turn away.

Why cooperative rather than an exception thrown at the loop from outside:

* **What has been done stays done.** A stop between rounds keeps the draft the
  model has written, the artifacts earlier stages produced, and the change
  journal that ``/undo`` needs (DR-024). Killing the thread mid-write keeps
  none of that reliably.
* **One mechanism, every Adapter.** The TUI sets it from a signal handler, the
  dashboard from an HTTP request on another thread, a test by calling
  ``request()``. None of them needs to know how a stage is implemented.
* **It cannot be missed.** ``threading.Event`` makes the flag visible across
  threads without a lock at each read, which is what lets the dashboard's
  request reach a turn already running on a worker thread.

A token is reusable: :meth:`clear` resets it, so a session arms one token once
and clears it at the start of every turn instead of rebuilding the plumbing.
"""

from __future__ import annotations

import threading

__all__ = ["CancelToken"]


class CancelToken:
    """A thread-safe, resettable "please stop" flag with a reason attached."""

    __slots__ = ("_event", "_lock", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    def request(self, reason: str = "") -> bool:
        """Ask the run to stop. Returns whether *this* call was the first.

        The return value is what lets an Adapter escalate: the first ctrl-c
        asks politely and gets ``True``, a second one gets ``False`` and can
        abort outright instead of appearing to do nothing.
        """
        with self._lock:
            first = not self._event.is_set()
            if first:
                self._reason = reason.strip()
            self._event.set()
        return first

    def clear(self) -> None:
        """Re-arm the token, discarding any earlier request."""
        with self._lock:
            self._reason = ""
            self._event.clear()

    @property
    def requested(self) -> bool:
        """Whether a stop has been asked for and not yet cleared."""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """Why the stop was asked for, or ``""`` when no reason was given."""
        with self._lock:
            return self._reason

    def wait(self, timeout: float | None = None) -> bool:
        """Block until a stop is requested, or *timeout* elapses.

        For a caller that would otherwise sleep — a poller, a queue wait —
        so it wakes on a stop instead of after its next tick.
        """
        return self._event.wait(timeout)

    def __bool__(self) -> bool:
        return self._event.is_set()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        state = f"requested {self._reason!r}" if self.requested else "armed"
        return f"<CancelToken {state}>"
