"""Print a reproducible random subset of DeepSWE task names.

Pier's --sample-seed shuffles tasks in directory-listing order, which differs
between filesystems, so the same seed can pick different tasks on different
machines. Sorting first makes the subset depend only on the seed.

Usage: python sample_tasks.py <deep-swe/tasks> [--n 20] [--seed 42]
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path


def sample(tasks_dir: Path, n: int, seed: int) -> list[str]:
    names = sorted(
        p.name
        for p in tasks_dir.iterdir()
        if (p / "task.toml").is_file() and (p / "instruction.md").is_file()
    )
    random.Random(seed).shuffle(names)
    return names[:n]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks_dir", type=Path)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print("\n".join(sample(args.tasks_dir, args.n, args.seed)))


if __name__ == "__main__":
    main()
