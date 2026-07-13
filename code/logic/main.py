import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from tiers import TIERS

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "profiles" / "config.yaml"


def _user_config_path() -> Path | None:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return Path(base) / "AutoSE" / "config.yaml" if base else None
    return Path.home() / ".config" / "autose" / "config.yaml"


def _find_config() -> Path:
    env_value = os.environ.get("AUTOSE_CONFIG")
    if env_value:
        env_path = Path(env_value)
        if env_path.exists():
            return env_path

    user_config = _user_config_path()
    if user_config is not None and user_config.exists():
        return user_config

    if _DEFAULT_CONFIG.exists():
        return _DEFAULT_CONFIG

    locations = "\n".join(
        f"  - {location}"
        for location in (
            "$AUTOSE_CONFIG (environment variable)",
            user_config or "%APPDATA%\\AutoSE\\config.yaml",
            _DEFAULT_CONFIG,
        )
    )
    raise FileNotFoundError(
        "Config file not found. Checked:\n"
        f"{locations}\n"
        "Create it by copying profiles/config.yaml and filling in your inference settings."
    )


def _resolve_workspace_root(config_path: Path) -> Path:
    """Resolve the workspace root AutoSE should operate on.

    If `workspace.root` is set to a non-empty value in config.yaml, that
    path is used (expanding `~` and resolving it to an absolute path).
    Otherwise, the directory the command was run from (the current working
    directory) is used.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    configured_root = (config.get("workspace") or {}).get("root")
    if configured_root:
        return Path(str(configured_root)).expanduser().resolve()
    return Path.cwd()


def _import_session():
    code_dir = str(Path(__file__).resolve().parents[1])
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    from tui.session import run
    return run


def _classify_mode(config_path: Path, prompt: str) -> str:
    code_dir = str(Path(__file__).resolve().parents[1])
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    from classifier import TaskClassifier

    try:
        return TaskClassifier(config_path).classify(prompt)
    except Exception:
        return "lite"


def _run_headless(args: argparse.Namespace, config_path: Path, prompt: str) -> None:
    code_dir = str(Path(__file__).resolve().parents[1])
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    from core.clients import run_headless

    mode = args.mode
    if mode == "auto":
        mode = _classify_mode(config_path, prompt)

    def event_sink(event: dict) -> None:
        print(json.dumps({"type": "event", "event": event}), flush=True)

    result = run_headless(
        prompt,
        config_path=config_path,
        workspace_root=args.workspace,
        mode=mode,
        auto_approve_commands=args.yes,
        stream=args.stream and not args.json and not args.events,
        event_sink=event_sink if args.events else None,
    )

    if args.events:
        print(json.dumps({"type": "session", "payload": result}), flush=True)
    elif args.json:
        print(json.dumps(result, indent=2))
    elif not args.stream:
        print(result["result"]["summary"])


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="autose")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run without the interactive TUI",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="run headless and print structured JSON",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="stream assistant output in headless mode",
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="run headless and print structured JSONL events followed by the final session",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="approve terminal commands in headless mode",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", *TIERS),
        default="auto",
        help="workflow mode for headless runs",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help=(
            "workspace root to inspect or modify (default: workspace.root from "
            "config.yaml if set, otherwise the current working directory)"
        ),
    )
    parser.add_argument("prompt", nargs="*")
    return parser.parse_args(argv)


def main() -> None:
    config_path = _find_config()
    headless_flags = (
        "--headless",
        "--json",
        "--stream",
        "--events",
        "--yes",
        "--mode",
        "--workspace",
    )
    if not any(
        arg == flag or arg.startswith(f"{flag}=")
        for arg in sys.argv[1:]
        for flag in headless_flags
    ):
        initial_prompt = " ".join(sys.argv[1:]).strip()
        session_run = _import_session()
        session_run(
            config_path,
            workspace_root=_resolve_workspace_root(config_path),
            initial_prompt=initial_prompt,
        )
        return

    args = _parse_args(sys.argv[1:])
    initial_prompt = " ".join(args.prompt).strip()
    workspace_explicitly_set = args.workspace is not None
    if not workspace_explicitly_set:
        args.workspace = _resolve_workspace_root(config_path)
    if (
        args.json
        or args.stream
        or args.events
        or args.yes
        or args.mode != "auto"
        or workspace_explicitly_set
    ):
        args.headless = True

    if args.headless:
        if not initial_prompt:
            raise SystemExit("--headless/--json/--events requires a prompt.")
        _run_headless(args, config_path, initial_prompt)
        return


if __name__ == "__main__":
    main()
