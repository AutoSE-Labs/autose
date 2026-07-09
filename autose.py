"""Entry-point wrapper — run from the repo root with: uv run autose.py"""
import sys
from pathlib import Path

_LOGIC = Path(__file__).parent / "code" / "logic"
_CODE  = Path(__file__).parent / "code"

for _p in (str(_LOGIC), str(_CODE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from main import main  # noqa: E402 — logic/main.py

if __name__ == "__main__":
    main()
