"""Running the dashboard as a long-lived local service (DR-047).

Two small things a service needs that a dashboard started by hand does not:

* **A token that survives a restart.** ``--web`` mints a fresh token every
  start, which is right for a window someone opened and will close. A service
  restarts on its own — after a crash, a reboot, an upgrade — and every
  bookmark and every notification link would die with the old token. In
  service mode the token is created once, kept owner-only in the user config
  directory, and reused. Deleting the file rotates it.
* **A unit file.** On Linux, ``--install-service`` *writes* a systemd user
  unit and prints the one command that enables it. It never runs
  ``systemctl`` itself: starting a background service that drives an agent is
  the person's decision to make, with the command in front of them.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from logic.user_config import atomic_write_text, user_config_dir

__all__ = [
    "TOKEN_FILENAME",
    "UNIT_NAME",
    "stable_token",
    "unit_path",
    "render_unit",
    "install_service",
]

TOKEN_FILENAME = "dashboard.token"  # noqa: S105 - a file name, not a secret
UNIT_NAME = "autose-dashboard.service"

#: Shortest token accepted from the file. Anything shorter was not written by
#: AutoSE (which writes 32 characters) and is replaced, not trusted.
_MIN_TOKEN_LENGTH = 24


def stable_token(directory: Path | None = None) -> str:
    """The service's token: read from the user config directory, or created once.

    Written 0600 from the first byte; an existing file with looser permissions
    is tightened, since it is AutoSE's own file and holds the only credential
    the dashboard has.
    """
    path = (directory or user_config_dir()) / TOKEN_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) >= _MIN_TOKEN_LENGTH and existing.isprintable() and " " not in existing:
        with contextlib.suppress(OSError):
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                path.chmod(0o600)
        return existing
    token = secrets.token_urlsafe(24)
    atomic_write_text(path, token + "\n", mode=0o600)
    return token


def unit_path() -> Path:
    """``$XDG_CONFIG_HOME/systemd/user/autose-dashboard.service``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / UNIT_NAME


def _quote(argument: str) -> str:
    """One ``ExecStart`` argument, quoted the way systemd reads it.

    ``%`` starts a specifier in a unit file, so it is doubled; quotes and
    backslashes are escaped inside double quotes.
    """
    text = argument.replace("%", "%%")
    if text and not any(ch in text for ch in ' \t"\\\'$;'):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(
    *,
    python: str,
    workspace: Path,
    port: int,
    host: str,
    pythonpath: str = "",
) -> str:
    """The text of the unit file: the dashboard in service mode, restarted on failure."""
    command = " ".join(
        _quote(part)
        for part in (
            python,
            "-m",
            "gui",
            "--service",
            "--workspace",
            str(workspace),
            "--host",
            host,
            "--port",
            str(port),
        )
    )
    environment = ["Environment=PYTHONUNBUFFERED=1"]
    if pythonpath:
        environment.append(f"Environment={_quote('PYTHONPATH=' + pythonpath)}")
    lines = [
        "# Written by `autose --install-service`. Edit freely; run it again to",
        "# regenerate. Enable with: systemctl --user enable --now autose-dashboard",
        "[Unit]",
        f"Description=AutoSE dashboard for {str(workspace).replace('%', '%%')}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        # A path setting takes the rest of the line as is; only `%` is special.
        f"WorkingDirectory={str(workspace).replace('%', '%%')}",
        f"ExecStart={command}",
        *environment,
        "Restart=on-failure",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def install_service(
    *,
    workspace: Path,
    port: int,
    host: str,
    platform: str | None = None,
    python: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Write the unit file and say how to enable it. Returns an exit code.

    Never starts, enables or reloads anything.
    """
    platform = platform or sys.platform
    if not platform.startswith("linux"):
        out(
            "--install-service writes a systemd user unit, which is Linux only; "
            f"it is not supported here ({platform}). Run "
            "`autose --web --service` from your own startup mechanism instead."
        )
        return 2
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        out(f"--install-service: {root} is not a directory")
        return 2
    target = unit_path()
    text = render_unit(
        python=python or sys.executable,
        workspace=root,
        port=port,
        host=host,
        pythonpath=os.environ.get("PYTHONPATH", ""),
    )
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, text, mode=0o644)
    out(f"{'updated' if existed else 'wrote'} {target}")
    out(f"  serves {root} on {host}:{port}, with a token kept in {user_config_dir() / TOKEN_FILENAME}")
    out("Nothing has been started. To run it now and at every login:")
    out("  systemctl --user daemon-reload")
    out("  systemctl --user enable --now autose-dashboard")
    out("Its address (with the token) is in: journalctl --user -u autose-dashboard")
    return 0
