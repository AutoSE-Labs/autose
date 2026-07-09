from collections.abc import Iterator
from pathlib import Path
from typing import Union

import yaml

from .code.agent import CodeAgent
from .plan.agent import PlanAgent
from .test.agent import TestAgent


def run(
    prompt: str,
    config_path: Union[str, Path],
    workspace_root: Union[str, Path] = ".",
) -> Iterator[str]:
    """
    Sequential Plan → Code → Test pipeline.

    1. PlanAgent  explores the codebase and produces an implementation plan.
    2. CodeAgent  reads the plan and applies all file changes.
    3. TestAgent  writes & runs tests, then streams the final report.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    inference = config.get("inference", {})
    kwargs = dict(
        base_url=inference.get("base_url", "http://127.0.0.1:11434/v1"),
        api_key=inference.get("api_key", "") or "",
        model=inference.get("model", ""),
        workspace_root=str(workspace_root),
    )

    # ------------------------------------------------------------------
    # Stage 1 — Plan
    # ------------------------------------------------------------------
    yield "[Plan] Analysing codebase and building implementation plan...\n"
    plan = PlanAgent(**kwargs).run(prompt)
    yield f"\n[Plan] Done.\n\n{plan}\n\n"

    # ------------------------------------------------------------------
    # Stage 2 — Code
    # ------------------------------------------------------------------
    yield "[Code] Implementing the plan...\n"
    code_summary = CodeAgent(**kwargs).run(prompt, plan)
    yield f"\n[Code] Done.\n\n{code_summary}\n\n"

    # ------------------------------------------------------------------
    # Stage 3 — Test
    # ------------------------------------------------------------------
    yield "[Test] Writing and running tests...\n\n"
    yield from TestAgent(**kwargs).run(prompt, plan, code_summary)
    yield "\n"
