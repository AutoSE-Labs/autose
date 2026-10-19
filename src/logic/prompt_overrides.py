"""User-editable overrides for AutoSE's built-in prompts (DR-021).

Every prompt AutoSE sends has a name and a built-in default written alongside
the code that sends it. A repo's ``.autose/config.yaml`` ``prompts:`` section
may replace any of them, by that name, with its own text. Nothing here
decides what a default *is* — each module still owns its own — this only
resolves a name to whichever text should actually be used.
"""

from __future__ import annotations

from collections.abc import Mapping

from .errors import ConfigurationError

__all__ = ["PromptOverrides", "KNOWN_PROMPT_NAMES", "resolve_prompt", "format_prompt"]

#: A prompt name -> replacement text mapping, as loaded from config.
PromptOverrides = Mapping[str, str]

#: Every overridable prompt, named exactly as a config file's ``prompts:``
#: section must spell it. Documented here so a config author has one place to
#: look, rather than hunting through source for what name a prompt answers to.
KNOWN_PROMPT_NAMES: tuple[str, ...] = (
    "classifier_system",
    "classifier_user",
    "spark_system",
    "design",
    "plan",
    "execute",
    "validate",
)


def resolve_prompt(name: str, default: str, overrides: PromptOverrides | None) -> str:
    """Return the configured override for *name*, or *default* if none is set.

    A blank or whitespace-only override is treated as absent rather than as a
    deliberate empty prompt — an empty string in a config file almost always
    means "I didn't fill this in yet," not "send nothing."
    """
    if not overrides:
        return default
    override = overrides.get(name)
    return override if isinstance(override, str) and override.strip() else default


def format_prompt(template: str, **values: object) -> str:
    """Fill *template*'s placeholders, failing predictably on a bad one.

    DR-021 lets a config author replace prompt text freely, and a stray ``{``
    or a misspelled field name in what they write is a config mistake, not a
    reason to crash the run with a raw traceback.

    Every way ``str.format`` can reject hand-written text is caught, not just
    the unknown-field case: an unmatched brace raises ``ValueError``, an
    attribute or format spec raises ``AttributeError``/``ValueError``, and a
    stray ``{`` is by far the likeliest of these mistakes. All of them are the
    same class of error to the person who has to fix it, so all of them get
    the same actionable message naming the offending prompt text.
    """
    try:
        return template.format(**values)
    except KeyError as exc:
        # A KeyError's argument *is* the field name, so it names the offender
        # precisely. Every other failure carries a sentence instead, and is
        # reported as a bad template below rather than dressed up as a name.
        placeholder = exc.args[0] if exc.args else exc
        raise ConfigurationError(
            f"prompt has an unresolvable placeholder: {{{placeholder}}}. "
            f"Known placeholders here: {_known(values)}. "
            "Double a literal brace to escape it: {{ }}."
        ) from exc
    except (ValueError, IndexError, AttributeError, TypeError) as exc:
        raise ConfigurationError(
            f"prompt is not a valid template: {exc}. "
            f"Known placeholders here: {_known(values)}. "
            "Double a literal brace to escape it: {{ }}."
        ) from exc


def _known(values: Mapping[str, object]) -> str:
    return ", ".join(f"{{{name}}}" for name in sorted(values)) or "(none)"
