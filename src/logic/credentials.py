"""API keys, kept in their own owner-only file (DR-036).

A hosted provider needs a key; a local one does not (DR-015). Keys are stored
apart from ``config.yaml`` on purpose: that file is meant to be read, copied,
pasted into an issue and — for the repository's ``.autose/config.yaml`` —
committed. A secret in it is one ``git add`` from being published.

So keys live in ``credentials.yaml`` beside the user's config file
(``~/.config/autose`` or ``%APPDATA%/AutoSE``), written atomically and owner-only
(0600, in a 0700 directory when AutoSE creates it). Nothing outside this module
reads the file, and nothing it hands an adapter for display contains a key:
:meth:`CredentialStore.view` returns whether one exists and its last four
characters, never the value.

Where a key comes from, for a provider:

1. the credentials file;
2. the provider's conventional environment variable, when it has one
   (``OLLAMA_API_KEY`` for Ollama Cloud), declared on its
   :class:`~logic.inference.registry.Provider` rather than listed here.

``AUTOSE_API_KEY`` and a legacy ``inference.api_key`` keep working for the
provider in the config file; :mod:`logic.config` applies them.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError
from .inference.registry import available_providers
from .user_config import atomic_write_text, read_yaml_mapping, user_config_dir

__all__ = [
    "CREDENTIALS_FILENAME",
    "CredentialStore",
    "KeyView",
    "mask_key",
    "validate_api_key",
]

CREDENTIALS_FILENAME = "credentials.yaml"

#: Longer than any real key, short enough that a pasted file is refused.
_MAX_KEY_LENGTH = 4096

_HEADER = """\
# AutoSE API keys. Written by AutoSE; readable by you alone (0600).
# Never commit or share this file. Delete a key from the dashboard's settings,
# or remove its entry here.
"""


@dataclass(frozen=True)
class KeyView:
    """What may be shown about a key: that it exists, and a recognisable tail."""

    has_key: bool
    #: e.g. ``"••••3f9a"``; empty when there is no key.
    masked: str = ""
    #: ``"stored"``, ``"environment"``, ``"config"``, or ``""`` for none.
    source: str = ""


def mask_key(key: str) -> str:
    """The last four characters behind a fixed-width mask.

    A short key shows the mask alone: four characters of an eight-character
    secret is half of it.
    """
    if not key:
        return ""
    return "••••" + (key[-4:] if len(key) >= 12 else "")


def validate_api_key(key: str) -> str:
    """Return *key* stripped, or refuse a value that cannot be a header."""
    value = (key or "").strip()
    if not value:
        raise ConfigurationError("the API key is empty")
    if len(value) > _MAX_KEY_LENGTH:
        raise ConfigurationError("that is too long to be an API key")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        # Whitespace or a control character inside a key is a paste error at
        # best, and a header-injection attempt at worst.
        raise ConfigurationError("an API key cannot contain spaces or control characters")
    return value


class CredentialStore:
    """Load, save and delete one key per provider name."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        # Resolved late, so a test (or a user) that moves the config directory
        # after this object was built still gets the right file.
        return self._path or user_config_dir() / CREDENTIALS_FILENAME

    # -- reading -----------------------------------------------------------

    def _keys(self) -> dict[str, str]:
        document = read_yaml_mapping(self.path)
        section = document.get("providers")
        if not isinstance(section, dict):
            return {}
        keys: dict[str, str] = {}
        for name, entry in section.items():
            value = entry.get("api_key") if isinstance(entry, dict) else None
            if isinstance(value, str) and value.strip():
                keys[str(name).strip().lower()] = value.strip()
        return keys

    def stored(self, provider: str) -> str:
        """The key saved for *provider*, or ``""``. Never logged, never displayed."""
        try:
            return self._keys().get(_name(provider), "")
        except ConfigurationError:
            # A corrupt file must not take the dashboard down with it; it reads
            # as "no key", and saving a key rewrites the file.
            return ""

    def environment(self, provider: str) -> str:
        """The key in *provider*'s conventional environment variable, if any."""
        registered = available_providers().get(_name(provider))
        variable = registered.api_key_env if registered is not None else ""
        return os.environ.get(variable, "").strip() if variable else ""

    def resolve(self, provider: str) -> str:
        """The key to use for *provider*: stored first, then its env variable."""
        return self.stored(provider) or self.environment(provider)

    def view(self, provider: str, *, configured: str = "") -> KeyView:
        """What a settings page may show. *configured* is a key from config/env."""
        stored = self.stored(provider)
        if stored:
            return KeyView(True, mask_key(stored), "stored")
        environment = self.environment(provider)
        if environment:
            return KeyView(True, mask_key(environment), "environment")
        if configured:
            return KeyView(True, mask_key(configured), "config")
        return KeyView(False)

    def providers(self) -> tuple[str, ...]:
        """Names with a stored key."""
        try:
            return tuple(sorted(self._keys()))
        except ConfigurationError:
            return ()

    # -- writing -----------------------------------------------------------

    def save(self, provider: str, key: str) -> None:
        """Store *key* for *provider*, replacing any previous one."""
        name = _name(provider)
        if not name:
            raise ConfigurationError("which provider is this key for?")
        value = validate_api_key(key)
        with self._lock:
            keys = self._keys_or_empty()
            keys[name] = value
            self._write(keys)

    def delete(self, provider: str) -> bool:
        """Forget *provider*'s key. Returns whether there was one."""
        name = _name(provider)
        with self._lock:
            keys = self._keys_or_empty()
            if name not in keys:
                return False
            del keys[name]
            self._write(keys)
        return True

    def _keys_or_empty(self) -> dict[str, str]:
        try:
            return self._keys()
        except ConfigurationError:
            # Unreadable: start over rather than refuse to save forever. The
            # broken file is replaced atomically, not appended to.
            return {}

    def _write(self, keys: dict[str, str]) -> None:
        import yaml

        document = {
            "providers": {name: {"api_key": keys[name]} for name in sorted(keys)}
        }
        atomic_write_text(
            self.path,
            _HEADER + yaml.safe_dump(document, sort_keys=False),
            mode=0o600,
        )


def _name(provider: str) -> str:
    return (provider or "").strip().lower()
