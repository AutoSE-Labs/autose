"""Shared core primitives for AutoSE clients."""

from .clients import HeadlessClient, run_headless
from .session import (
    SessionArtifact,
    SessionEvent,
    SessionResult,
    TaskSessionRecorder,
)

__all__ = [
    "HeadlessClient",
    "SessionArtifact",
    "SessionEvent",
    "SessionResult",
    "TaskSessionRecorder",
    "run_headless",
]
