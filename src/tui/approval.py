"""The terminal's answer to DR-012's approval question.

An ``ApprovalGate`` implementation, which is all an Adapter has to supply: it
shows the concrete call and blocks on an answer. The rule that side-effecting
tools *must* be gated lives in `logic/`, where every interface inherits it —
this module only decides what the question looks like in a terminal.

The prompt is deliberately free-form rather than a fixed choice set. ``y``,
``n`` and ``a`` are the shortcuts, but anything else the user types is read as
a *reason*: the call is refused and the sentence travels to the model on the
denial (see :func:`logic.common.approval.denial_result`), so "no, use ripgrep
instead" redirects the run rather than merely stopping it.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from logic.common.allowances import Allowance, offer_for, standing_for
from logic.common.approval import ApprovalDecision, ApprovalRequest

from .render import approval_panel
from .theme import THEME

__all__ = ["TerminalApprovalGate"]

#: Answers taken as "run this one call". Empty input means the same: the
#: prompt's default is shown on the panel, so a bare Enter is an informed yes.
_ALLOW_ONCE: frozenset[str] = frozenset({"y", "yes", "ok", "okay", "sure", "run", "allow"})

#: Answers taken as "run calls like this unasked for the rest of the session":
#: the same command words, the same host, or the same tool (DR-048).
_ALLOW_ALWAYS: frozenset[str] = frozenset({"a", "always", "all"})

#: Answers taken as a bare refusal, with nothing to pass back to the model.
_DENY: frozenset[str] = frozenset({"n", "no", "nope", "deny", "skip", "cancel", "stop"})


class TerminalApprovalGate:
    """Prompts the user, and remembers an "always allow" if given (DR-048)."""

    def __init__(
        self, console: Console, *, stop_requested: Callable[[], bool] | None = None
    ) -> None:
        self._console = console
        self._always_allow: dict[str, Allowance] = {}
        #: Whether the run is already stopping (DR-030). Without this, a ctrl-c
        #: pressed while an approval prompt is up looks ignored: the first press
        #: asks the run to stop, but the run is blocked on this prompt and
        #: cannot reach a point where it would notice.
        self._stop_requested = stop_requested or (lambda: False)

    @property
    def always_allowed(self) -> tuple[str, ...]:
        """What runs unasked, as phrases: "pytest … commands"."""
        return tuple(item.label for item in self._always_allow.values())

    def reset(self) -> None:
        """Forget every standing allowance — used when the session changes."""
        self._always_allow.clear()

    def watch_for_stop(self, stop_requested: Callable[[], bool]) -> None:
        """Attach the session's stop signal, once the session exists."""
        self._stop_requested = stop_requested

    def request(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._stop_requested():
            # Deny rather than prompt, and say why: a standing "always allow"
            # must not run a command either, since the user has asked the run
            # to stop and this call has not started yet.
            self._console.print()
            self._console.print(
                Text(
                    f"  {THEME.warn_glyph} not running {request.tool_name} — "
                    "you asked this run to stop",
                    style=THEME.warning,
                )
            )
            return ApprovalDecision.deny(
                "the user stopped this run before the call was made"
            )

        standing = standing_for(request, self._always_allow.values())
        if standing is not None:
            return ApprovalDecision.allow(
                f"{standing.label} were allowed for the rest of this session"
            )

        self._console.print()
        self._console.print(approval_panel(request))
        try:
            answer = Prompt.ask(
                f"  [{THEME.warning}]allow?[/] [{THEME.muted}](y/n/a or a reason)[/]",
                default="y",
                show_default=False,
                console=self._console,
            )
        except (EOFError, KeyboardInterrupt):
            # A cancelled prompt is a denial, never an approval.
            self._console.print()
            return ApprovalDecision.deny("the user interrupted the approval prompt")

        return self._decide(request, answer)

    def _decide(self, request: ApprovalRequest, answer: str | None) -> ApprovalDecision:
        reply = (answer or "").strip()
        keyword = reply.lower().rstrip(".!,")

        if not reply or keyword in _ALLOW_ONCE:
            return ApprovalDecision.allow("the user approved this call")
        if keyword in _ALLOW_ALWAYS:
            offer = offer_for(request)
            if offer.allowance is None:
                # The person said yes; only the "from now on" part cannot be
                # given. Run this one call and say why the next will ask.
                self._console.print(
                    Text(
                        f"  {THEME.warn_glyph} running it once; calls like this will "
                        f"still ask: {offer.reason}",
                        style=THEME.warning,
                    )
                )
                return ApprovalDecision.allow("the user approved this call")
            # DR-052: a chain's yes is a yes for each of its parts.
            for part in offer.every:
                self._always_allow[part.key] = part
            self._console.print(
                Text(
                    f"  {THEME.ok_glyph} {offer.label} will run unasked for the "
                    "rest of this session (/tools to review)",
                    style=THEME.muted,
                )
            )
            return ApprovalDecision.allow(
                f"the user allowed {offer.label} for the rest of this session"
            )
        if keyword in _DENY:
            return ApprovalDecision.deny("the user declined this call")

        # Anything else is guidance. It is still a refusal — nothing runs — but
        # the model is told *why*, so it can take the other route instead of
        # re-proposing the call it was just refused.
        self._console.print(
            Text(f"  {THEME.bullet} sent to the model: {reply}", style=THEME.muted)
        )
        return ApprovalDecision.deny(
            f"the user declined this call and said: {reply}"
        )

    @staticmethod
    def prompt_hint() -> str:
        return (
            "y = run once   n = skip   a = allow calls like this all session   "
            "anything else = decline with a reason the model will see"
        )
