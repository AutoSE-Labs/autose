from pathlib import Path
from typing import Union

import yaml

from .agent import PlanAgent


def run(
    prompt: str,
    config_path: Union[str, Path],
    workspace_root: Union[str, Path] = ".",
) -> str:
    """Run the Plan agent standalone and return the implementation plan."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    inference = config.get("inference", {})
    agent = PlanAgent(
        base_url=inference.get("base_url", "http://127.0.0.1:11434/v1"),
        api_key=inference.get("api_key", "") or "",
        model=inference.get("model", ""),
        workspace_root=str(workspace_root),
    )
    return "".join(agent.run(prompt))
