"""Colours and glyphs for the terminal interface.

Presentation only (DR-002). Kept in one module so the interface reads as one
thing rather than as a scatter of inline style strings, and so a future theme
is a value swap rather than an edit across every renderer.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Theme", "THEME"]


@dataclass(frozen=True)
class Theme:
    """One coherent set of styles."""

    accent: str = "bright_cyan"
    accent_dim: str = "cyan"
    user: str = "bright_white"
    assistant: str = "white"
    thinking: str = "grey42"
    tool: str = "bright_blue"
    tool_dim: str = "blue"
    success: str = "bright_green"
    warning: str = "yellow"
    danger: str = "bright_red"
    muted: str = "grey50"
    rule: str = "grey30"

    # Glyphs are ASCII-adjacent on purpose: they survive a plain terminal,
    # an SSH session, and a log file paste.
    prompt: str = "›"
    continuation: str = "⋮"
    bullet: str = "•"
    tool_glyph: str = "⚒"
    think_glyph: str = "…"
    ok_glyph: str = "✓"
    fail_glyph: str = "✗"
    warn_glyph: str = "!"
    approval_glyph: str = "▲"


THEME = Theme()
