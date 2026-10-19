"""The per-user configuration directory, and the one write AutoSE makes to it.

Every other file in that directory is the user's to edit; AutoSE only reads
them (DR-015). Two things are written by AutoSE itself, both from the
dashboard's settings (DR-036): the credentials file (:mod:`logic.credentials`)
and the *default* provider and model for new sessions, which lands in the
user's own ``config.yaml`` under ``inference:`` so the terminal and the
dashboard start from the same place.

Writes are atomic — a temporary file in the same directory, then
``os.replace`` — so a crash mid-write leaves the old file, never half of one.
A YAML file a person wrote is changed key by key, keeping its comments, by
:func:`update_yaml_scalars` (DR-059).
"""

from __future__ import annotations

import contextlib
import copy
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .yaml_edit import Scalar, set_scalars

__all__ = [
    "USER_CONFIG_FILENAME",
    "user_config_dir",
    "user_config_file",
    "atomic_write_text",
    "ensure_private_dir",
    "read_yaml_mapping",
    "save_default_inference",
    "update_yaml_scalars",
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
    return _parse_mapping(_read_text(path), path)


def _read_text(path: Path) -> str:
    """The file's text; empty when it does not exist."""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc


def _parse_mapping(text: str, path: Path) -> dict[str, Any]:
    import yaml

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def update_yaml_scalars(
    path: Path,
    updates: Mapping[str, Scalar],
    *,
    new_file_mode: int = 0o600,
) -> bool:
    """Set dotted keys in the YAML file at *path*, keeping its comments (DR-059).

    ``{"inference.model": "qwen3:8b"}`` sets ``model`` under ``inference``;
    a missing key or parent is added. Every other key, comment, blank line and
    quote in the file stays as it was: the text is edited line by line
    (:func:`logic.yaml_edit.set_scalars`), and the edit is kept only if
    parsing it gives exactly the document a plain load, update and dump would
    have written.

    When it does not — the file uses flow style (``{a: 1}``), anchors or
    aliases, tags, block scalars or several documents where the change has to
    go, or anything else the line editor will not vouch for — the whole file
    is rewritten with ``yaml.safe_dump`` instead. That is still correct, but
    loses the comments. Returns whether they were kept.

    A new file is created with *new_file_mode* (owner-only unless the caller
    says otherwise); an existing one keeps its mode. The write is atomic.
    """
    import yaml

    text = _read_text(path)
    expected = _with_updates(_parse_mapping(text, path), updates, path)
    edited = set_scalars(text, updates)
    kept = edited is not None and _means(edited, expected)
    output = (
        edited
        if kept and edited is not None
        else yaml.safe_dump(expected, sort_keys=False, allow_unicode=True)
    )
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else new_file_mode
    atomic_write_text(path, output, mode=mode)
    return kept


def _with_updates(
    document: dict[str, Any], updates: Mapping[str, Scalar], path: Path
) -> dict[str, Any]:
    """*document* with *updates* applied, as a load-update-dump would leave it."""
    result = copy.deepcopy(document)
    for dotted, value in updates.items():
        keys = dotted.split(".")
        node = result
        for depth, key in enumerate(keys[:-1]):
            child = node.get(key)
            if child is None:
                child = node[key] = {}
            if not isinstance(child, dict):
                where = ".".join(keys[: depth + 1])
                raise ConfigurationError(f"{path}: '{where}' must be a mapping")
            node = child
        node[keys[-1]] = value
    return result


def _means(text: str, expected: dict[str, Any]) -> bool:
    """Whether *text* parses to exactly *expected*."""
    import yaml

    try:
        return bool(yaml.safe_load(text) == expected)
    except yaml.YAMLError:
        return False


def save_default_inference(
    provider: str,
    model: str,
    base_url: str = "",
    *,
    path: Path | None = None,
) -> Path:
    """Record the provider and model new sessions start with (DR-036).

    Only ``inference.provider``, ``inference.base_url`` and ``inference.model``
    are written; every other key in the file, and its comments, are kept
    (DR-059: :func:`update_yaml_scalars`, which falls back to a full rewrite
    only for a file it cannot edit safely). An API key is never written here —
    keys live in the credentials file — and one the user put here by hand is
    left exactly as it was.

    This touches the *user* file only, never a repository's committed
    ``.autose/config.yaml`` (DR-036).
    """
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
    update_yaml_scalars(
        target,
        {
            "inference.provider": provider,
            "inference.base_url": base_url,
            "inference.model": model,
        },
    )
    return target
