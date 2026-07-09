import sys
from pathlib import Path

# repo root is three levels up: src/autose/cli.py -> src/autose -> src -> repo
_ROOT  = Path(__file__).resolve().parents[2]
_LOGIC = _ROOT / "code" / "logic"
_CODE  = _ROOT / "code"

for _p in (str(_LOGIC), str(_CODE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from main import main  # noqa: E402 — code/logic/main.py


def run() -> None:
    main()
