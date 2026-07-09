"""
tbench.py — Run AutoSE on Terminal-Bench 2.1 via Harbor.

Usage (from repo root):
    uv run benchmarks/tbench.py [options]

Options:
    --dataset   Harbor dataset identifier  (default: terminal-bench@2.1)
    --n         Number of concurrent tasks (default: 1)
    --task      Run a single task by name  (optional)
    --dry-run   Print the harbor command and exit

Logs are written to:  benchmarks/results/tbench/<run-id>/
Harbor per-trial agent logs go to each trial's logs_dir inside that run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ─────────────────────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
_RESULTS_DIR = _REPO / "benchmarks" / "results" / "tbench"


# ── Harbor installation check ─────────────────────────────────────────────────


def _ensure_harbor() -> str:
    """Return the harbor executable path, installing it with uv if absent."""
    path = shutil.which("harbor")
    if path:
        return path

    print("[tbench] harbor not found — installing via 'uv tool install harbor'...")
    result = subprocess.run(
        ["uv", "tool", "install", "harbor"],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(
            "[tbench] ERROR: failed to install harbor. Install manually: uv tool install harbor"
        )

    path = shutil.which("harbor")
    if not path:
        sys.exit(
            "[tbench] ERROR: harbor was installed but is not on PATH.\n"
            "  Run:  uv tool install harbor\n"
            "  Then: uv run benchmarks/tbench.py"
        )
    return path


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AutoSE on Terminal-Bench 2.1 via Harbor.",
    )
    parser.add_argument(
        "--dataset",
        default="terminal-bench@2.1",
        help="Harbor dataset identifier (default: terminal-bench@2.1)",
    )
    parser.add_argument(
        "-n",
        "--n-concurrent",
        type=int,
        default=1,
        help="Number of tasks to run in parallel (default: 1)",
    )
    parser.add_argument(
        "--task",
        help="Run only a specific task name (optional).",
    )
    parser.add_argument(
        "--mode",
        choices=("standard", "lite"),
        default="standard",
        help="Choose the benchmark agent mode (default: standard).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the harbor command and exit without running.",
    )
    args = parser.parse_args()

    harbor = _ensure_harbor()

    # ── Output directory ──────────────────────────────────────────────────
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = run_dir / "run.log"

    print(f"[tbench] run-id  : {run_id}")
    print(f"[tbench] results : {run_dir}")
    print(f"[tbench] dataset : {args.dataset}")
    print(f"[tbench] workers : {args.n_concurrent}")
    print(f"[tbench] mode    : {args.mode}")
    if args.task:
        print(f"[tbench] task    : {args.task}")

    # ── Agent import path ─────────────────────────────────────────────────
    # Harbor resolves this as a Python module path.
    # We add the repo root to PYTHONPATH so 'benchmarks.autose_agent' is importable.
    agent_import_path = {
        "standard": "benchmarks.autose_agent:AutoSEAgent",
        "lite": "benchmarks.autose_agent:LiteAutoSEAgent",
    }[args.mode]

    # ── Build harbor run command ──────────────────────────────────────────
    cmd = [
        harbor,
        "run",
        "--dataset",
        args.dataset,
        "--agent-import-path",
        agent_import_path,
        "--n-concurrent",
        str(args.n_concurrent),
        "--jobs-dir",
        str(run_dir),
    ]
    if args.task:
        cmd += ["--task", args.task]

    print(f"\n[tbench] command : {' '.join(cmd)}\n")

    if args.dry_run:
        return

    # ── Environment: add repo root to PYTHONPATH ─────────────────────────
    env = os.environ.copy()
    python_path = str(_REPO)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{python_path}{os.pathsep}{existing}" if existing else python_path
    )

    # ── Run ───────────────────────────────────────────────────────────────
    print(f"[tbench] logging to {log_file}\n{'-' * 60}")
    with open(log_file, "w", encoding="utf-8") as lf:
        lf.write(f"command: {' '.join(cmd)}\n\n")

    try:
        proc = subprocess.run(
            cmd,
            env=env,
            cwd=str(_REPO),
        )
    except KeyboardInterrupt:
        print("\n[tbench] interrupted by user")
        sys.exit(1)

    print(f"\n{'-' * 60}")
    print(f"[tbench] harbor exited with code {proc.returncode}")
    print(f"[tbench] results saved to {run_dir}")

    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
