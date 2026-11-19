"""Summarise a Pier job directory: solved tasks, wall time, and tokens per task.

Usage: python report.py <jobs-dir>/<job-name> [--csv out.csv] [--md out.md]
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


def _seconds(timing: dict | None) -> float | None:
    if not timing or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    start = datetime.fromisoformat(timing["started_at"])
    end = datetime.fromisoformat(timing["finished_at"])
    return (end - start).total_seconds()


def _row(result: dict) -> dict:
    agent = result.get("agent_result") or {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    exception = result.get("exception_info") or {}
    reward = rewards.get("reward")
    return {
        "task": result.get("task_name", ""),
        "solved": bool(reward) and float(reward) >= 1.0,
        "reward": reward,
        "f2p": f"{rewards.get('f2p_passed', '-')}/{rewards.get('f2p_total', '-')}",
        "p2p": f"{rewards.get('p2p_passed', '-')}/{rewards.get('p2p_total', '-')}",
        "agent_min": round((_seconds(result.get("agent_execution")) or 0) / 60, 1),
        "total_min": round((_seconds(result) or 0) / 60, 1),
        "input_tokens": agent.get("n_input_tokens"),
        "output_tokens": agent.get("n_output_tokens"),
        "tool_calls": agent.get("n_agent_steps"),
        "error": exception.get("exception_type", ""),
    }


def load(job_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(job_dir.glob("*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if "task_name" in result:
            rows.append(_row(result))
    return rows


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def markdown(rows: list[dict]) -> str:
    cols = list(rows[0].keys()) if rows else []
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_fmt(r[c]) for c in cols) + " |" for r in rows]
    solved = sum(r["solved"] for r in rows)
    tot = lambda key: sum(r[key] or 0 for r in rows)  # noqa: E731
    lines.append("")
    lines.append(f"Solved {solved}/{len(rows)}. Agent time {tot('agent_min'):.0f} min, "
                 f"input tokens {tot('input_tokens'):,}, output tokens {tot('output_tokens'):,}.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--md", type=Path)
    args = parser.parse_args()

    rows = load(args.job_dir)
    text = markdown(rows)
    print(text)
    if args.md:
        args.md.write_text(text + "\n", encoding="utf-8")
    if args.csv and rows:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
