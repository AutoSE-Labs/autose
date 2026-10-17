"""The terminal Adapter over ``src/logic`` (DR-002).

Input handling, rendering, and wiring — nothing else. Ships in every edition
(DR-013).
"""

from __future__ import annotations

from .app import App, run

__all__ = ["App", "run"]
