"""Finding a local runtime without being told where one is (DR-015).

A user who already has Ollama running should never be asked for a base URL —
every value that question asks for is one AutoSE can find out. Probing runs
against the conventional ports of the registered local providers, in parallel,
with a short timeout, so a machine with nothing running costs a moment rather
than a stall.

Probing only reads: it asks each candidate what it serves and reports the
answers. It never writes configuration, never starts anything, and a failed
probe is a reported absence, not an error.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..errors import AutoSEError
from .catalog import ModelInfo
from .registry import Provider, create_model_catalog, local_providers
from .settings import InferenceSettings

__all__ = ["DetectedRuntime", "probe_runtime", "discover_local_runtimes"]


@dataclass(frozen=True)
class DetectedRuntime:
    """The outcome of probing one candidate endpoint."""

    provider: str
    base_url: str
    reachable: bool
    models: tuple[ModelInfo, ...] = ()
    #: Why the probe failed, when it did. Shown to the user instead of a bare
    #: "connection refused", so a wrong port is distinguishable from a runtime
    #: that is running but serving nothing.
    error: str = ""
    description: str = ""

    @property
    def has_models(self) -> bool:
        return bool(self.models)

    @property
    def usable(self) -> bool:
        """Reachable *and* actually serving something we could run."""
        return self.reachable and self.has_models

    def settings(self, model: str = "") -> InferenceSettings:
        """Build settings targeting this runtime, defaulting to its first model.

        The model's published context length rides along, so DR-010's budget
        starts from a measured limit rather than an assumption.
        """
        chosen = model or (self.models[0].name if self.models else "")
        info = next((m for m in self.models if m.name == chosen), None)
        return InferenceSettings(
            base_url=self.base_url,
            model=chosen,
            provider=self.provider,
            context_limit=info.context_length if info else None,
        )

    def model(self, name: str) -> ModelInfo | None:
        return next((info for info in self.models if info.name == name), None)

    def summary(self) -> str:
        if not self.reachable:
            return f"{self.provider} at {self.base_url}: not reachable"
        if not self.models:
            return f"{self.provider} at {self.base_url}: running, but no models available"
        return (
            f"{self.provider} at {self.base_url}: "
            f"{len(self.models)} model(s) — {', '.join(m.name for m in self.models[:3])}"
            + (" …" if len(self.models) > 3 else "")
        )


def probe_runtime(
    provider: Provider,
    *,
    base_url: str = "",
    timeout: float = 1.5,
) -> DetectedRuntime:
    """Ask one candidate endpoint what it serves. Never raises."""
    url = base_url or provider.default_base_url
    if not url:
        return DetectedRuntime(
            provider=provider.name,
            base_url="",
            reachable=False,
            error="no conventional address to probe; configure base_url explicitly",
            description=provider.description,
        )
    settings = InferenceSettings(
        base_url=url, provider=provider.name, discovery_timeout=timeout
    )
    try:
        models = create_model_catalog(settings).list_models()
    except AutoSEError as exc:
        return DetectedRuntime(
            provider=provider.name,
            base_url=settings.base_url,
            reachable=False,
            error=str(exc),
            description=provider.description,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never break startup
        return DetectedRuntime(
            provider=provider.name,
            base_url=settings.base_url,
            reachable=False,
            error=f"unexpected probe failure: {exc}",
            description=provider.description,
        )
    return DetectedRuntime(
        provider=provider.name,
        base_url=settings.base_url,
        reachable=True,
        models=models,
        description=provider.description,
    )


def discover_local_runtimes(
    *, timeout: float = 1.5, include_unreachable: bool = False
) -> tuple[DetectedRuntime, ...]:
    """Probe every registered local provider at once.

    Results are ordered most-useful-first: runtimes actually serving models,
    then those merely reachable, then (only if asked for) the rest.
    """
    providers = local_providers()
    if not providers:
        return ()
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        results = list(
            pool.map(lambda provider: probe_runtime(provider, timeout=timeout), providers)
        )
    if not include_unreachable:
        results = [runtime for runtime in results if runtime.reachable]
    return tuple(
        sorted(results, key=lambda r: (not r.usable, not r.reachable, r.provider))
    )
