"""AutoSE computation layer.

Per DR-002, *all* business logic lives under ``src/logic/``. The ``tui`` and
``gui`` packages are thin Adapters over the public entry points exposed here;
nothing in this package may import from either of them.

This module deliberately re-exports nothing heavyweight — importing
``logic`` must stay cheap so an Adapter can probe capabilities without
paying for the whole agent stack.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.2.2"
