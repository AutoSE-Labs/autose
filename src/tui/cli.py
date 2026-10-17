"""Command-line entry point for the terminal interface.

This is the composition root: it parses arguments and wires an Adapter to
``logic``. It lives in ``src/tui/`` rather than ``src/logic/`` precisely
because it imports an interface — DR-002's dependency direction makes that a
presentation-side concern.

``--web`` hands off to the dashboard adapter, which is the one thing here that
may not exist: ``src/gui/`` must stay deletable (DR-029). The hand-off is
therefore resolved *by name at call time* (see :func:`_run_dashboard`) and
never imported, so this module carries no dependency on the adapter it starts
and a build without it answers the flag with a sentence instead of a
traceback (DR-029).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from logic import __version__
from logic.tiers import TIERS

__all__ = ["main", "parse_args"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="autose",
        description=(
            "A local-first software engineering agent. Point it at any "
            "OpenAI-compatible endpoint; by default it finds one already "
            "running on this machine."
        ),
    )
    parser.add_argument("prompt", nargs="*", help="an opening request (optional)")
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
        "--doctor",
        action="store_true",
        help="report what this machine can run, and exit",
    )
    parser.add_argument(
        "--yes",
        "--auto-approve",
        dest="auto_approve",
        action="store_true",
        help=(
            "approve shell and network calls without asking, for this run only. "
            "For unattended use; the run is recorded as auto-approved by you."
        ),
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="ID",
        help=(
            "pick up a stored session: the most recent one, or the id (or a "
            "unique prefix of it) shown by --sessions"
        ),
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="list the sessions stored for this workspace, and exit",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help=(
            "run one prompt without the interactive interface and write "
            "newline-delimited JSON to stdout. Exits non-zero if the run did "
            "not complete. Requires a prompt."
        ),
    )
    parser.add_argument(
        "--tier",
        choices=TIERS,
        default=None,
        help="force a tier instead of classifying the prompt",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help=(
            "serve the web dashboard and open it in a browser. Same agent, "
            "same workspace, same approval prompts — in a page instead of a "
            "terminal."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        metavar="ADDRESS",
        help=(
            "with --web, the address to bind (default: loopback). Anything "
            "else exposes an agent that can write to your workspace."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="with --web, the port to bind, or 0 to let the OS choose",
    )
    parser.add_argument(
        "--no-browser",
        dest="open_browser",
        action="store_false",
        help="with --web, print the URL instead of opening a browser",
    )
    parser.add_argument("--version", action="version", version=f"autose {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.doctor:
        from .doctor import print_report

        return print_report()

    if args.sessions:
        from .app import print_sessions

        return print_sessions(workspace=args.workspace)

    prompt = " ".join(args.prompt)
    if args.web:
        if prompt:
            # Silently dropping a request the user typed would be worse than
            # refusing it: the dashboard takes its prompts in the browser.
            print(
                "--web opens the dashboard, which takes its requests in the "
                f'browser. Drop the prompt, or run: autose "{prompt}"',
                file=sys.stderr,
            )
            return 2
        return _run_dashboard(args)

    if args.as_json:
        from .headless import run_headless

        return run_headless(
            prompt,
            config_path=args.config,
            workspace=args.workspace,
            auto_approve=args.auto_approve,
            authorized_by=_invoking_user(),
            tier=args.tier,
            resume=args.resume,
        )

    from .app import run

    return run(
        config_path=args.config,
        workspace=args.workspace,
        initial_prompt=prompt,
        auto_approve=args.auto_approve,
        tier=args.tier,
        resume=args.resume,
    )


def _run_dashboard(args: argparse.Namespace) -> int:
    """Start the dashboard adapter, if this build ships one (DR-029).

    Resolved by name rather than imported, and that is the whole point: the
    adapter must stay deletable with nothing else changed, so this module must
    not depend on the package it hands off to, and the flag has to degrade
    into an explanation when the package is absent.

    The dashboard's own argument parser is reused rather than reimplemented —
    its defaults for host, port and browser-opening stay stated in one place.
    """
    from importlib import import_module
    from importlib.util import find_spec

    if find_spec("gui") is None:
        print(
            "this build has no web dashboard (src/gui/ is missing); the "
            "terminal and JSON interfaces still work. Reinstall AutoSE to "
            "get the dashboard back.",
            file=sys.stderr,
        )
        return 2

    forwarded: list[str] = []
    if args.config is not None:
        forwarded += ["--config", str(args.config)]
    if args.workspace is not None:
        forwarded += ["--workspace", str(args.workspace)]
    if args.tier:
        forwarded += ["--tier", args.tier]
    if args.auto_approve:
        forwarded.append("--yes")
    if args.resume:
        forwarded += ["--resume", args.resume]
    if args.host:
        forwarded += ["--host", args.host]
    if args.port is not None:
        forwarded += ["--port", str(args.port)]
    if not args.open_browser:
        forwarded.append("--no-browser")
    exit_code: int = import_module("gui.cli").main(forwarded)
    return exit_code


def _invoking_user() -> str:
    """Who an unattended `--json --yes` run is attributed to (DR-012)."""
    import getpass
    import os

    try:
        return f"{getpass.getuser()} (--json --yes)"
    except Exception:  # noqa: BLE001 - no controlling user in some environments
        return f"{os.environ.get('USER', 'unknown')} (--json --yes)"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
