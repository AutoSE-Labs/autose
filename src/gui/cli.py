"""``python -m gui`` — the dashboard's composition root.

The same job ``tui/cli.py`` does for the terminal: parse arguments, build the
one thing ``logic`` needs handed to it, and start the interface. It is a
separate entry point rather than a flag on ``autose`` precisely so that
deleting ``src/gui/`` leaves nothing dangling behind (DR-029) — the terminal
CLI does not know this module exists, and a test asserts it cannot.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
import threading
import webbrowser
from dataclasses import replace
from pathlib import Path

from logic import __version__
from logic.config import AutoSEConfig, load_config
from logic.errors import ConfigurationError
from logic.tiers import TIERS

from . import DEFAULT_HOST, DEFAULT_PORT

__all__ = ["main", "parse_args", "serve"]

EXIT_OK = 0
EXIT_CONFIGURATION = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m gui",
        description=(
            "AutoSE's web dashboard: the same agent as the terminal interface, "
            "in a browser. Serves on this machine only unless told otherwise."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "address to bind (default: %(default)s). Anything other than "
            "loopback exposes an agent that can write to your workspace — the "
            "startup token is then the only thing between it and the network."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port to bind, or 0 to let the OS choose (default: %(default)s)",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="path to a config file"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="directory to work in (default: the current directory)",
    )
    parser.add_argument(
        "--tier",
        choices=TIERS,
        default=None,
        help="force a tier instead of classifying each prompt",
    )
    parser.add_argument(
        "--yes",
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        help=(
            "approve shell and network calls without asking, for this run only. "
            "The dashboard then never interrupts, and the run is recorded as "
            "auto-approved by you."
        ),
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="ID",
        help="open a stored session: the most recent one, or an id prefix",
    )
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="print the URL instead of opening a browser",
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help=(
            "run as a long-lived local service: never open a browser, and keep "
            "one token across restarts (stored owner-only in your AutoSE config "
            "directory) so bookmarks keep working"
        ),
    )
    parser.add_argument(
        "--install-service",
        dest="install_service",
        action="store_true",
        help=(
            "write a systemd user unit that runs this dashboard as a service "
            "for --workspace on --port, print the command to enable it, and "
            "exit. Linux only; nothing is started."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every HTTP request to stderr",
    )
    parser.add_argument(
        "--version", action="version", version=f"autose dashboard {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.install_service:
        from .service import install_service

        return install_service(
            workspace=Path(args.workspace) if args.workspace is not None else Path.cwd(),
            port=args.port,
            host=args.host,
        )
    try:
        config = load_config(args.config, workspace_root=args.workspace)
    except ConfigurationError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION
    if args.workspace is not None or args.auto_approve:
        # DR-012: unattended approval is a deliberate, attributed opt-in, and
        # it is attributed to whoever started the dashboard — not to the
        # browser tab that later happens to be pointed at it.
        config = replace(
            config,
            workspace_root=(
                Path(args.workspace) if args.workspace is not None else config.workspace_root
            ),
            auto_approve=config.auto_approve or args.auto_approve,
            auto_approve_authorized_by=(
                config.auto_approve_authorized_by or _invoking_user()
            ),
        )
    for message in config.warnings:
        print(f"warning: {message}", file=sys.stderr)

    return serve(
        config,
        config_path=args.config,
        host=args.host,
        port=args.port,
        tier=args.tier,
        resume=args.resume,
        open_browser=args.open_browser,
        verbose=args.verbose,
        service=args.service,
    )


def serve(
    config: AutoSEConfig,
    *,
    config_path: str | Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    tier: str | None = None,
    resume: str | None = None,
    open_browser: bool = True,
    verbose: bool = False,
    service: bool = False,
) -> int:
    """Run the dashboard until interrupted. Returns a process exit code.

    With *service* (DR-047) the token is the stable one from the user config
    directory and no browser is opened: nobody is sitting in front of a
    service when it starts.
    """
    # Imported here rather than at module scope so `--help` and a bad config
    # never pay for the session stack.
    from .bridge import SessionBridge
    from .server import create_dashboard

    bridge = SessionBridge(
        config, config_path=config_path, default_tier=tier, resume=resume
    )
    if service:
        from .service import stable_token

        token = stable_token()
        open_browser = False
    else:
        token = secrets.token_urlsafe(24)
    try:
        dashboard = create_dashboard(
            bridge, host=host, port=port, token=token, verbose=verbose
        )
    except OSError as exc:
        print(f"cannot serve on {host}:{port} — {exc}", file=sys.stderr)
        return EXIT_CONFIGURATION

    workspace = config.resolve_workspace_root()
    print(f"AutoSE dashboard {__version__}")
    print(f"  workspace  {workspace}")
    print(f"  open       {dashboard.url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  warning    this is reachable from the network. Anyone with the "
            "URL can drive an agent inside that workspace."
        )
    if service:
        print("  token      kept across restarts; delete the token file to rotate it")
    print("  stop       ctrl-c")
    # The URL carries the token, and it is the one line the operator cannot do
    # without. Piping this into a file or a log makes stdout block-buffered, so
    # flush rather than leave it sitting in a buffer until the server stops.
    sys.stdout.flush()
    opening = bridge.snapshot()
    if not opening["ready"]:
        # Not fatal: the panel in the browser explains it and offers a retry,
        # which is the difference between "start Ollama and look again" and
        # "read this traceback".
        print(f"  note       no model yet — {opening['problem']}")
    if open_browser:
        threading.Thread(
            target=webbrowser.open, args=(dashboard.url,), daemon=True
        ).start()

    # Schedules and watchers tick from here on, for as long as it serves.
    bridge.start_automation()
    try:
        dashboard.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        dashboard.stop()
        dashboard.server_close()
    return EXIT_OK


def _invoking_user() -> str:
    """Who an auto-approved run is attributed to (DR-012)."""
    try:
        return f"{getpass.getuser()} (dashboard --yes)"
    except Exception:  # noqa: BLE001 - no controlling user in some environments
        return f"{os.environ.get('USER', 'unknown')} (dashboard --yes)"
