"""Every provider AutoSE knows about, and what each can run right now (DR-036).

The settings page and the model picker both need one answer: for each
registered provider, is it configured, is it reachable, does it have a key,
and which models does it serve. :func:`list_connections` gives it, probing
every provider at once with the short discovery timeout, so a dead port or a
slow hosted API costs one timeout in total rather than one each.

A provider that fails is a row saying why, never an exception: one broken
endpoint must not hide the five that work. A provider that needs a key and has
none is not contacted at all. No key ever leaves this module unmasked.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from .credentials import CredentialStore, KeyView
from .errors import ConfigurationError
from .inference.catalog import ModelInfo
from .inference.discovery import probe_runtime
from .inference.registry import Provider, available_providers, get_provider
from .inference.settings import DEFAULT_DISCOVERY_TIMEOUT, InferenceSettings

__all__ = ["Connection", "list_connections", "provider_settings"]


@dataclass(frozen=True)
class Connection:
    """One provider as the settings page shows it. Holds no secret."""

    provider: str
    description: str
    is_local: bool
    requires_api_key: bool
    key: KeyView
    base_url: str
    #: Whether a listing was attempted and answered.
    reachable: bool
    models: tuple[ModelInfo, ...] = ()
    #: Why it is not reachable, or why it was not probed.
    error: str = ""
    #: Whether AutoSE chooses this provider's window (DR-035).
    serves_requested_window: bool = False

    @property
    def usable(self) -> bool:
        return self.reachable and bool(self.models)


def list_connections(
    *,
    credentials: CredentialStore | None = None,
    base_urls: Mapping[str, str] | None = None,
    configured_keys: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
) -> tuple[Connection, ...]:
    """Probe every registered provider in parallel; never raises.

    *base_urls* overrides a provider's conventional address (the configured
    one, or one a session is using). *configured_keys* are keys that came from
    the config file or ``AUTOSE_API_KEY`` rather than the credentials file;
    they are used for probing and shown only as masked.
    """
    store = credentials or CredentialStore()
    urls = {name.lower(): url for name, url in (base_urls or {}).items() if url}
    extra = {name.lower(): key for name, key in (configured_keys or {}).items() if key}
    providers = sorted(
        available_providers().values(),
        # Local first (DR-015), then by name.
        key=lambda provider: (not provider.is_local, provider.name),
    )
    if not providers:
        return ()

    def one(provider: Provider) -> Connection:
        try:
            return _connection(
                provider,
                store=store,
                base_url=urls.get(provider.name, ""),
                configured_key=extra.get(provider.name, ""),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - one provider must not hide the rest
            return Connection(
                provider=provider.name,
                description=provider.description,
                is_local=provider.is_local,
                requires_api_key=provider.requires_api_key,
                key=KeyView(False),
                base_url=urls.get(provider.name, "") or provider.default_base_url,
                reachable=False,
                error=f"could not be checked: {exc}",
                serves_requested_window=provider.serves_requested_window,
            )

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        return tuple(pool.map(one, providers))


def _connection(
    provider: Provider,
    *,
    store: CredentialStore,
    base_url: str,
    configured_key: str,
    timeout: float,
) -> Connection:
    url = (base_url or provider.default_base_url).rstrip("/")
    view = store.view(provider.name, configured=configured_key)
    key = store.resolve(provider.name) or configured_key
    row = Connection(
        provider=provider.name,
        description=provider.description,
        is_local=provider.is_local,
        requires_api_key=provider.requires_api_key,
        key=view,
        base_url=url,
        reachable=False,
        serves_requested_window=provider.serves_requested_window,
    )
    if not url:
        return replace(row, error="no conventional address; set a base_url to use it")
    if provider.requires_api_key and not key:
        # Not contacted: a request without a key can only fail, and it would
        # tell the service someone is here for nothing.
        return replace(row, error="add an API key to list its models")
    probed = probe_runtime(provider, base_url=url, timeout=timeout, api_key=key)
    return replace(
        row,
        reachable=probed.reachable,
        models=probed.models,
        error=probed.error,
    )


def provider_settings(
    base: InferenceSettings,
    provider: str,
    model: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    credentials: CredentialStore | None = None,
) -> InferenceSettings:
    """Settings for *model* on *provider*, carrying *base*'s tuning (DR-036).

    The address is *base_url*, else *base*'s when the provider is unchanged,
    else the provider's conventional one. The key is *api_key*, else the
    stored or environment key, else *base*'s when the provider is unchanged.
    Refuses — a :class:`ConfigurationError` naming what is missing — an
    unknown provider, one with no address, and one that needs a key and has
    none, so a switch fails at the click rather than mid-turn.
    """
    registered = get_provider(provider)
    name = registered.name
    same = name == (base.provider or "").strip().lower()
    url = (base_url or "").strip() or (base.base_url if same else "") or (
        registered.default_base_url
    )
    if not url:
        raise ConfigurationError(
            f"{name} has no conventional address; give a base_url to use it"
        )
    key = api_key if api_key is not None else (
        (credentials or CredentialStore()).resolve(name) or (base.api_key if same else "")
    )
    if registered.requires_api_key and not key:
        raise ConfigurationError(
            f"{name} needs an API key. Add one in the dashboard's settings"
            + (f" or set {registered.api_key_env}" if registered.api_key_env else "")
            + "."
        )
    return replace(
        base,
        provider=name,
        base_url=url,
        api_key=key,
        model=model,
        # A window is a fact about one model on one runtime; the new one is
        # decided afresh by `adopt_context_window`.
        context_limit=None,
    )
