"""What "allow this for the rest of the session" covers (DR-048).

An approval answers one call (DR-012). Saying yes to the same kind of call
again and again teaches people to stop reading, so every adapter offers a
standing yes for the session. This module decides how wide that yes is, once,
for every adapter:

* ``run_command`` — the command's leading words: ``pytest``, ``python -m
  pytest``, ``git status``, ``npm run test``. A later command is covered only
  when its own leading words come out the same, so allowing ``git status``
  does not allow ``git push``. Allowing the whole tool would allow any command
  at all.
* ``fetch_url`` — the host: "pages on docs.python.org".
* anything else (``web_search``, and the edit tools when a workspace gates
  them) — the tool, as before.

Some commands are never covered by a standing yes and must be answered one
call at a time: anything using shell syntax (``;``, ``&&``, pipes, redirects,
``$(…)``), wrappers that run an arbitrary command (``sudo``, ``env``,
``xargs``, ``sh -c``, ``python -c``), and commands that destroy or publish
(``rm``, ``chmod``, ``git push``). These are word checks, not a sandbox: they
narrow what one click allows, and the call is still recorded as an event.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from .approval import ApprovalRequest

__all__ = ["Allowance", "AllowanceOffer", "offer_for"]


@dataclass(frozen=True)
class Allowance:
    """A standing yes for one session: a tool, narrowed by *pattern*."""

    tool: str
    #: The command's leading words, or the host; empty for the whole tool.
    pattern: str = ""

    @property
    def key(self) -> str:
        return f"{self.tool}:{self.pattern}"

    @property
    def label(self) -> str:
        """What it covers, as a phrase: "pytest … commands"."""
        if self.tool == "run_command":
            return f"{self.pattern} … commands"
        if self.tool == "fetch_url":
            return f"pages on {self.pattern}"
        return _TOOL_PHRASES.get(self.tool, f"{self.tool.replace('_', ' ')} calls")

    def covers(self, request: ApprovalRequest) -> bool:
        if request.tool_name != self.tool:
            return False
        # Re-derived from the new call rather than compared by prefix, so every
        # rule that refused to offer a standing yes also refuses to match one:
        # `pytest; rm -rf .` has no pattern, so no allowance covers it.
        offered = offer_for(request).allowance
        return offered is not None and offered.pattern == self.pattern

    def to_dict(self) -> dict[str, str]:
        return {"id": self.key, "tool": self.tool, "pattern": self.pattern, "label": self.label}


@dataclass(frozen=True)
class AllowanceOffer:
    """What a standing yes for this call would cover, or why there is none."""

    allowance: Allowance | None
    #: Why only "once" is offered, when ``allowance`` is None.
    reason: str = ""


_TOOL_PHRASES = {
    "web_search": "web searches",
    "write_file": "file writes",
    "edit_file": "file edits",
}

#: Shell syntax that chains, pipes, redirects, substitutes or backgrounds. A
#: command using any of it is several things at once, and the leading words
#: no longer describe what it does.
_SHELL_SYNTAX = re.compile(r"[;&|<>`\n\r]|\$[({]")

#: Commands that run whatever command they are given.
_WRAPPERS = frozenset(
    {
        "sudo", "doas", "su", "env", "xargs", "nohup", "timeout", "watch", "nice",
        "ionice", "time", "exec", "eval", "command", "builtin", "source", ".",
        "sh", "bash", "zsh", "dash", "fish", "ksh", "ssh", "parallel", "script",
        "busybox", "chroot", "unshare", "flock", "stdbuf", "setsid", "strace",
    }
)

#: Commands whose next word is the program they run: the script or module is
#: part of what was allowed, and an inline program (`-c`, `-e`) never is.
_INTERPRETERS = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|ruby|perl|php|lua|Rscript|"
    r"npx|bunx|uvx|pipx|java|dotnet)$"
)

#: Flags with which an interpreter only reports on itself and exits.
_INFO_FLAGS = frozenset({"--version", "-V", "-VV", "--help", "-h"})

#: Commands with subcommands: the subcommand is part of what was allowed.
_SUBCOMMANDS = frozenset(
    {
        "git", "npm", "pnpm", "yarn", "uv", "cargo", "go", "pip", "pip3", "poetry",
        "pipenv", "hatch", "pdm", "conda", "mamba", "make", "just", "gh", "docker",
        "podman", "kubectl", "helm", "terraform", "mvn", "gradle", "rustup", "brew",
        "apt", "apt-get", "dnf", "pacman", "systemctl", "journalctl", "nix", "mix",
        "bundle", "rake", "composer", "swift", "zig", "tox", "nox", "ruff", "mypy",
    }
)

#: Subcommands that run the command after them; that command is part of the
#: pattern in turn (`uv run pytest`, `npm run test`).
_RUNNERS = frozenset(
    {
        ("uv", "run"), ("uv", "tool"), ("npm", "run"), ("npm", "exec"), ("pnpm", "run"),
        ("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "run"), ("yarn", "exec"), ("yarn", "dlx"),
        ("poetry", "run"), ("pipenv", "run"), ("hatch", "run"), ("pdm", "run"),
        ("conda", "run"), ("mamba", "run"), ("nix", "run"), ("nix", "shell"),
        ("bundle", "exec"),
    }
)

#: Subcommands that build and run the project itself, whatever their flags.
_PROJECT_RUNNERS = frozenset({("go", "run"), ("cargo", "run"), ("cargo", "test"), ("go", "test")})

#: Subcommands that run an arbitrary container or remote command.
_ARBITRARY = frozenset(
    {
        ("docker", "run"), ("docker", "exec"), ("podman", "run"), ("podman", "exec"),
        ("kubectl", "exec"), ("kubectl", "run"), ("gh", "api"), ("git", "config"),
    }
)

#: Commands that delete, overwrite permissions, stop processes or publish.
#: One call at a time, always.
_DESTRUCTIVE = frozenset(
    {
        "rm", "rmdir", "dd", "shred", "mkfs", "chmod", "chown", "chgrp", "kill",
        "pkill", "killall", "shutdown", "reboot", "truncate", "curl", "wget",
    }
)
_DESTRUCTIVE_SUBCOMMANDS = frozenset(
    {
        ("git", "push"), ("git", "reset"), ("git", "clean"), ("git", "rebase"),
        ("git", "checkout"), ("git", "restore"), ("git", "rm"), ("git", "branch"),
        ("git", "tag"), ("git", "stash"), ("git", "filter-branch"), ("npm", "publish"),
        ("pnpm", "publish"), ("yarn", "publish"), ("cargo", "publish"), ("uv", "publish"),
        ("poetry", "publish"), ("gh", "release"), ("gh", "pr"), ("docker", "push"),
        ("docker", "rm"), ("docker", "rmi"), ("kubectl", "delete"), ("terraform", "apply"),
        ("terraform", "destroy"), ("pip", "uninstall"), ("pip3", "uninstall"),
        ("systemctl", "stop"), ("systemctl", "disable"),
    }
)

#: `find` actions that run or delete.
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete"})


def offer_for(request: ApprovalRequest) -> AllowanceOffer:
    """The standing yes this call could be given, or why it can only be once."""
    tool = request.tool_name
    if tool == "run_command":
        command = request.arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return AllowanceOffer(None, "there is no command to describe")
        words, reason = _command_pattern(command)
        if words is None:
            return AllowanceOffer(None, reason)
        return AllowanceOffer(Allowance(tool, " ".join(words)))
    if tool == "fetch_url":
        url = request.arguments.get("url")
        host = urlparse(url).hostname if isinstance(url, str) else None
        if not host:
            return AllowanceOffer(None, "the address has no host")
        return AllowanceOffer(Allowance(tool, host.lower()))
    return AllowanceOffer(Allowance(tool))


def _command_pattern(command: str) -> tuple[list[str] | None, str]:
    if _SHELL_SYNTAX.search(command):
        return None, "it chains, pipes or redirects commands (;, &&, |, >, $(…))"
    try:
        words = shlex.split(command)
    except ValueError:
        return None, "it could not be split into words"
    if not words:
        return None, "it is empty"
    if "=" in words[0] and not words[0].startswith("="):
        return None, "it sets environment variables first"
    if words[0] == "find" and _FIND_ACTIONS & set(words):
        return None, "find with -exec or -delete runs or deletes things"
    return _pattern_from(words)


def _pattern_from(words: list[str]) -> tuple[list[str] | None, str]:
    program = words[0]
    name = program.rsplit("/", 1)[-1]
    rest = words[1:]
    if name in _WRAPPERS:
        return None, f"{name} runs whatever command it is given"
    if name in _DESTRUCTIVE:
        return None, f"{name} can only be allowed one call at a time"
    if _INTERPRETERS.match(name):
        if not rest:
            return None, f"{name} with no script runs whatever it reads"
        if rest[0] == "-m" and len(rest) > 1 and not rest[1].startswith("-"):
            if rest[1] in _SUBCOMMANDS:
                # `python -m pip install` is `pip install`, narrowed the same way.
                inner, reason = _pattern_from(rest[1:])
                return (None, reason) if inner is None else ([program, "-m", *inner], "")
            return [program, "-m", rest[1]], ""
        if rest[0] in _INFO_FLAGS:
            # `python3 --version` prints and exits; nothing it is given runs.
            return [program, rest[0]], ""
        if rest[0].startswith("-"):
            return None, f"{name} {rest[0]} runs code given inline or chosen by a flag"
        return [program, rest[0]], ""
    if name in _SUBCOMMANDS:
        if not rest:
            return [program], ""
        sub = rest[0]
        if sub.startswith("-"):
            # `git -C elsewhere …`, `npm --prefix …`: options before the
            # subcommand change what it acts on.
            return None, f"options before the {name} subcommand change what it acts on"
        if (name, sub) in _ARBITRARY:
            return None, f"{name} {sub} runs an arbitrary command"
        if (name, sub) in _DESTRUCTIVE_SUBCOMMANDS:
            return None, f"{name} {sub} can only be allowed one call at a time"
        if (name, sub) in _PROJECT_RUNNERS:
            return [program, sub], ""
        if (name, sub) in _RUNNERS:
            if len(rest) < 2:
                return [program, sub], ""
            if rest[1].startswith("-"):
                return None, f"options to {name} {sub} change what it runs"
            inner, reason = _pattern_from(rest[1:])
            if inner is None:
                return None, reason
            return [program, sub, *inner], ""
        return [program, sub], ""
    return [program], ""
