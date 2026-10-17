"""DR-021: user-editable overrides for AutoSE's built-in prompts."""

from __future__ import annotations

import pytest

from logic.errors import ConfigurationError
from logic.prompt_overrides import format_prompt, resolve_prompt


def test_an_absent_override_falls_back_to_the_default():
    assert resolve_prompt("design", "default text", None) == "default text"
    assert resolve_prompt("design", "default text", {}) == "default text"


def test_a_blank_override_is_treated_as_absent():
    assert resolve_prompt("design", "default text", {"design": "   "}) == "default text"


def test_a_configured_override_replaces_the_default():
    overrides = {"design": "custom text"}
    assert resolve_prompt("design", "default text", overrides) == "custom text"


def test_format_prompt_fills_placeholders():
    assert format_prompt("hello {name}", name="world") == "hello world"


def test_format_prompt_raises_a_configuration_error_on_an_unknown_field():
    """A user-written prompt mentioning `{braces}` must not raise a raw
    KeyError traceback that kills the session."""
    with pytest.raises(ConfigurationError, match="braces"):
        format_prompt("a prompt mentioning {braces} by mistake", workspace=".")


def test_an_unknown_field_names_the_placeholders_that_do_work():
    """Naming the offender is only half an actionable message; the author
    also needs to know what they were allowed to write."""
    with pytest.raises(ConfigurationError, match=r"\{workspace\}"):
        format_prompt("{oops}", workspace=".")


@pytest.mark.parametrize(
    ("label", "template"),
    [
        ("unmatched open brace", "workspace {workspace} and a stray { here"),
        ("unmatched close brace", "workspace {workspace} and a stray } here"),
        ("positional field", "workspace {workspace} and {0}"),
        ("attribute access", "workspace {workspace.parent}"),
        ("format spec", "workspace {workspace:d}"),
    ],
)
def test_every_malformed_template_is_a_configuration_error(label, template):
    """`str.format` rejects hand-written text in several distinct ways, and an
    unmatched brace is the likeliest of them. Each must surface as a config
    error the author can act on — never a raw traceback out of a live turn."""
    with pytest.raises(ConfigurationError):
        format_prompt(template, workspace=".")


def test_a_doubled_brace_still_escapes_normally():
    """The JSON-shaped default prompts rely on `{{ }}`; broadening the error
    handling must not break the escape that makes them work."""
    assert (
        format_prompt('{workspace} says {{"tier": "spark"}}', workspace="/w")
        == '/w says {"tier": "spark"}'
    )
