"""``python -m gui`` — start the dashboard.

The module entry point is deliberately the *only* documented launcher: a
console script declared in packaging metadata would survive the deletion of
this folder and fail at import time, which is exactly the coupling DR-029's
removable adapter must not have.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
