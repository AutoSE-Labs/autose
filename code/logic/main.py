import argparse
import json
import sys
from pathlib import Path

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "profiles" / "config.yaml"
_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def _find_config() -> Path:
    if _DEFAULT_CONFIG.exists():
        return _DEFAULT_CONFIG
    raise FileNotFoundError(
        f"Config file not found. Expected at: {_DEFAULT_CONFIG}\n"
        "Create it by copying profiles/config.yaml and filling in your inference settings."
    )


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
        choices=("auto", "lite", "standard"),
        default="auto",
        help="workflow mode for headless runs",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_WORKSPACE_ROOT,
        help="workspace root to inspect or modify (default: AutoSE repo root)",
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
            workspace_root=_WORKSPACE_ROOT,
            initial_prompt=initial_prompt,
        )
        return

    args = _parse_args(sys.argv[1:])
    initial_prompt = " ".join(args.prompt).strip()
    if (
        args.json
        or args.stream
        or args.events
        or args.yes
        or args.mode != "auto"
        or args.workspace != _WORKSPACE_ROOT
    ):
        args.headless = True

    if args.headless:
        if not initial_prompt:
            raise SystemExit("--headless/--json/--events requires a prompt.")
        _run_headless(args, config_path, initial_prompt)
        return


if __name__ == "__main__":
    main()
