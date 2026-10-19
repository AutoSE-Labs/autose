"""The per-user configuration directory, and the one write AutoSE makes to it.

Every other file in that directory is the user's to edit; AutoSE only reads
them (DR-015). Two things are written by AutoSE itself, both from the
dashboard's settings (DR-036): the credentials file (:mod:`logic.credentials`)
and the *default* provider and model for new sessions, which lands in the
user's own ``config.yaml`` under ``inference:`` so the terminal and the
dashboard start from the same place.

Writes are atomic — a temporary file in the same directory, then
``os.replace`` — so a crash mid-write leaves the old file, never half of one.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

__all__ = [
    "USER_CONFIG_FILENAME",
    "user_config_dir",
    "user_config_file",
    "atomic_write_text",
    "ensure_private_dir",
    "read_yaml_mapping",
    "save_default_inference",
]

USER_CONFIG_FILENAME = "config.yaml"

#: Owner-only. The directory holds the credentials file, so it is created
#: closed rather than relying on the umask (DR-036).
_DIRECTORY_MODE = 0o700


def user_config_dir() -> Path:
    """``%APPDATA%/AutoSE`` on Windows, else ``$XDG_CONFIG_HOME/autose``.

    ``$XDG_CONFIG_HOME`` defaults to ``~/.config``, per the XDG spec.
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "AutoSE"
        return Path.home() / "AppData" / "Roaming" / "AutoSE"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "autose"


def user_config_file() -> Path:
    return user_config_dir() / USER_CONFIG_FILENAME


def ensure_private_dir(directory: Path) -> None:
    """Create *directory* owner-only if it does not exist yet.

    An existing directory is left alone: it is the user's, and tightening the
    permissions of, say, a dotfiles checkout would be a surprise. The file
    inside is what carries the secret, and it is always written 0600.
    """
    if directory.exists():
        return
    directory.mkdir(parents=True, mode=_DIRECTORY_MODE, exist_ok=True)
    # `mkdir`'s mode is masked by the umask; say it again explicitly.
    with contextlib.suppress(OSError):
        directory.chmod(_DIRECTORY_MODE)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Replace *path* with *text* in one step, leaving it with *mode*.

    The temporary file is created by :func:`tempfile.mkstemp`, which opens it
    0600 from the first byte, so a secret is never readable by anyone else,
    not even for the moment between writing and renaming.
    """
    ensure_private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):  # no POSIX modes on some filesystems
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def read_yaml_mapping(path: Path) -> dict[str, Any]:
    """A YAML file's top-level mapping; empty when the file does not exist."""
    import yaml

    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def save_default_inference(
    provider: str,
    model: str,
    base_url: str = "",
    *,
    path: Path | None = None,
) -> Path:
    """Record the provider and model new sessions start with (DR-036).

    Only ``inference.provider``, ``inference.base_url`` and ``inference.model``
    are written; every other key in the file is kept. An API key is never
    written here — keys live in the credentials file — and one the user put
    here by hand is left exactly as it was.

    The file is round-tripped through ``yaml.safe_load``/``safe_dump``, so
    comments in it are lost. That is the price of not adding a round-tripping
    YAML dependency, and the reason this touches the *user* file only, never a
    repository's committed ``.autose/config.yaml``.
    """
    import yaml

    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    if not provider:
        raise ConfigurationError("a default needs a provider")
    if not model:
        raise ConfigurationError("a default needs a model")
    if not base_url:
        raise ConfigurationError(f"{provider} has no conventional address; give a base_url")
    target = path or user_config_file()
    document = read_yaml_mapping(target)
    section = document.get("inference")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigurationError(f"{target}: 'inference' must be a mapping")
    section.update({"provider": provider, "base_url": base_url, "model": model})
    document["inference"] = section
    existing_mode = (
        stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    )
    atomic_write_text(
        target,
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        mode=existing_mode,
    )
    return target
