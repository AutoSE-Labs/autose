from __future__ import annotations

import pytest

from logic.common.workspace import Workspace
from logic.inference import registry


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    """An isolated workspace root for tools that touch the filesystem."""
    return Workspace.at(tmp_path)


@pytest.fixture(autouse=True)
def isolated_provider_registry():
    """Undo any provider a test registered.

    The registry is process-global by design — registering a backend is meant
    to be a one-line import-time act (DR-006). That makes it shared mutable
    state between tests: a fake provider registered by one module stays
    registered for every module collected after it, and assertions about the
    *real* provider set then pass or fail depending on filename ordering.

    Snapshot and restore rather than clear, so tests keep seeing the built-in
    providers their imports registered.
    """
    saved = dict(registry._PROVIDERS)
    try:
        yield
    finally:
        registry._PROVIDERS.clear()
        registry._PROVIDERS.update(saved)
