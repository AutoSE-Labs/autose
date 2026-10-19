"""User-tunable knobs, loaded from a repo's ``.autose/config.yaml`` (DR-021).

Every numeric or behavioural constant that used to be a hardcoded
module-level default across ``src/logic`` is a field on one of the dataclasses
here instead, so a repo can change AutoSE's behaviour by editing one file
instead of patching source. Every field's default is exactly the value that
used to be hardcoded, so an absent or partial config changes nothing.

Grouped by the subsystem each knob tunes, not flattened, so a config file
reads as "context budgeting", "the classifier", "the shell tool" rather than
an undifferentiated list of numbers.

Every cap here reads ``-1`` as "no limit" (DR-030), and the ones that decide
*how long AutoSE keeps working* — tool rounds, validation loopbacks, format
retries, the wall clock — default to exactly that. The caps that remain are
the ones that decide *what fits in one request* (the context budget, tool
output bounding, session memory); those are physical constraints of the model
being served, not a judgement about when to give up, so they keep real
defaults while still accepting ``-1`` from a repo that knows better.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _typeshed import DataclassInstance

from .common.context import MIN_USABLE_CONTEXT_TOKENS
from .errors import ConfigurationError
from .limits import UNLIMITED

__all__ = [
    "ContextTuning",
    "ToolOutputTuning",
    "TierBounds",
    "ShellTuning",
    "EditingTuning",
    "ClassifierTuning",
    "SessionTuning",
    "Tuning",
]

#: Every section of `Tuning` is a frozen dataclass, and `_merged` relies on
#: that — it reads `fields()` and calls `replace()`. Bounding the variable
#: says so, rather than leaving a checker to assume any object will do.
_T = TypeVar("_T", bound="DataclassInstance")


@dataclass(frozen=True)
class ContextTuning:
    """DR-010's calibrated context budget."""

    safety_factor: float = 1.10
    reserved_output_fraction: float = 0.25
    reserved_output_min: int = 512
    reserved_output_max: int = 8192
    #: How many conversation rounds survive when the served context window is
    #: *unknown*. ``-1`` keeps every round (DR-030), which is only safe where
    #: the window really is large: with no limit to measure against, a runtime
    #: that quietly truncates from the front takes the system prompt with it
    #: (DR-022), and this cap is the only thing standing in the way.
    max_history_rounds: int = 20
    #: Served window below which a turn is refused outright rather than run
    #: (DR-022). Not a preference: under this figure the tool schemas, system
    #: prompt and reserved output leave too little for a single file read to
    #: survive to the next round, so the agent re-reads what the budget just
    #: evicted and is stopped as stalled — a failure that looks like a bad
    #: model and is really a bad window. ``-1`` runs anyway, for a caller who
    #: has measured their own setup and disagrees.
    min_usable_context_tokens: int = MIN_USABLE_CONTEXT_TOKENS


@dataclass(frozen=True)
class ToolOutputTuning:
    """DR-011's bounded, recoverable tool output.

    Kept bounded by default, unlike the caps in :class:`TierBounds`: this one
    is not deciding when to stop working, it is deciding how much of a 200 KB
    test log to put in a context window, and the full output stays readable
    through ``read_file`` either way. ``-1`` disables each (DR-030).
    """

    #: The ceiling, not the operative value: every agent narrows this to what
    #: its context window can actually carry between rounds
    #: (`ContextBudget.tool_result_char_allowance`). Sized for a comfortable
    #: local window rather than a 4k one, because the window is now what
    #: decides — leaving it low simply clipped output on machines with room
    #: to spare.
    max_chars: int = 8000
    max_artifacts: int = 20


@dataclass(frozen=True)
class TierBounds:
    """One tier's bounds: DR-009's loopback cap and time budget, plus how many
    tool-calling rounds one stage call may take.

    Every tier has ``max_tool_rounds``; only `craft` (multi-stage, per
    DR-003) use the other three — `spark` ignores them, since it has no
    validation loopback to bound.

    All four are unlimited by default (DR-030). They were 30/3/3600/2, sized
    against how a large hosted model works, and a local one of a few billion
    parameters does the same job in many more, smaller steps — so those figures
    stopped correct work partway through far more often than they caught a
    runaway loop. Set any of them to a number to bound it again; what stops a
    long run in the meantime is the person watching it.
    """

    max_tool_rounds: int = UNLIMITED
    max_validation_iterations: int = UNLIMITED
    time_budget_seconds: float = float(UNLIMITED)
    max_format_retries: int = UNLIMITED


@dataclass(frozen=True)
class ShellTuning:
    """The ``run_command`` tool.

    ``-1`` waits as long as the command takes (DR-030) — reasonable for a test
    suite or a build on slow hardware, and the reason it is not the default is
    that a command which never returns is the one thing a cancel token cannot
    interrupt: nothing checks it while the tool blocks.
    """

    command_timeout_seconds: float = 120.0


@dataclass(frozen=True)
class EditingTuning:
    """DR-007's batch edit tool. ``-1`` accepts a batch of any size."""

    max_edits_per_call: int = 20


@dataclass(frozen=True)
class ClassifierTuning:
    """DR-003's tier classifier."""

    #: ``-1`` sends no output cap at all, for a model that needs room to
    #: think its way to a one-word answer (DR-030).
    max_output_tokens: int = 200
    #: DR-020: classification needs no chain of thought. Empty disables the
    #: field entirely, for a provider where sending it is actively harmful.
    reasoning_effort: str = "none"
    max_history_messages: int = 4


@dataclass(frozen=True)
class SessionTuning:
    """Cross-turn session memory (DR-004)."""

    max_history_exchanges: int = 4
    max_summary_chars: int = 2_000


@dataclass(frozen=True)
class WorkflowTuning:
    """DR-009's loops for the `craft` staged workflow, unlimited by
    default (DR-030). Kept in step with the same fields on
    :class:`TierBounds`, which is what the runner actually reads."""

    max_validation_iterations: int = UNLIMITED
    time_budget_seconds: float = float(UNLIMITED)
    max_format_retries: int = UNLIMITED


@dataclass(frozen=True)
class Tuning:
    """Every tunable knob, as loaded from one config file's ``tuning:`` section."""

    context: ContextTuning = field(default_factory=ContextTuning)
    tool_output: ToolOutputTuning = field(default_factory=ToolOutputTuning)
    shell: ShellTuning = field(default_factory=ShellTuning)
    editing: EditingTuning = field(default_factory=EditingTuning)
    classifier: ClassifierTuning = field(default_factory=ClassifierTuning)
    session: SessionTuning = field(default_factory=SessionTuning)
    #: Still per tier, though every default is now unlimited (DR-030): the
    #: sections are the seam a repo bounds one tier through without bounding
    #: the other — "spark answers a question, cap it at 10 rounds; craft may
    #: take all night" is a config file, not a code change.
    spark: TierBounds = field(default_factory=TierBounds)
    craft: TierBounds = field(default_factory=TierBounds)
    forge: TierBounds = field(default_factory=TierBounds)

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any] | None) -> Tuning:
        """Build a :class:`Tuning`, merging each section over its defaults.

        Every key is optional; an absent section or an absent key inside one
        keeps that field's default, so a config file only ever needs to name
        what it wants to change.
        """
        doc = document or {}
        if not isinstance(doc, Mapping):
            raise ConfigurationError("config section 'tuning' must be a mapping")
        defaults = cls()
        return cls(
            context=_merged(defaults.context, doc.get("context"), "tuning.context"),
            tool_output=_merged(
                defaults.tool_output, doc.get("tool_output"), "tuning.tool_output"
            ),
            shell=_merged(defaults.shell, doc.get("shell"), "tuning.shell"),
            editing=_merged(defaults.editing, doc.get("editing"), "tuning.editing"),
            classifier=_merged(
                defaults.classifier, doc.get("classifier"), "tuning.classifier"
            ),
            session=_merged(defaults.session, doc.get("session"), "tuning.session"),
            spark=_merged(defaults.spark, doc.get("spark"), "tuning.spark"),
            craft=_merged(defaults.craft, doc.get("craft"), "tuning.craft"),
            forge=_merged(defaults.forge, doc.get("forge"), "tuning.forge"),
        )


def _merged(defaults: _T, overrides: Any, label: str) -> _T:
    if overrides is None:
        return defaults
    if not isinstance(overrides, Mapping):
        raise ConfigurationError(f"config section '{label}' must be a mapping")
    valid = {f.name for f in fields(defaults)}
    unknown = set(overrides) - valid
    if unknown:
        raise ConfigurationError(
            f"unknown key(s) in '{label}': {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(valid))}."
        )
    coerced: dict[str, Any] = {}
    for key, value in overrides.items():
        current = getattr(defaults, key)
        try:
            coerced[key] = (
                value if isinstance(value, type(current)) else type(current)(value)
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"'{label}.{key}' must be a {type(current).__name__}, got {value!r}"
            ) from exc
    return replace(defaults, **coerced)
