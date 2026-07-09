from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import time
from uuid import uuid4


@dataclass
class SessionEvent:
    type: str
    message: str = ""
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time)


@dataclass
class SessionArtifact:
    kind: str
    title: str
    path: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time)


@dataclass
class SessionResult:
    status: str = "running"
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    tests_run: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)


class TaskSessionRecorder:
    """Collects client-neutral execution events, artifacts, and outcomes."""

    def __init__(self, task: str, workspace_root: str, mode: str) -> None:
        self.session_id = str(uuid4())
        self.task = task
        self.workspace_root = str(Path(workspace_root))
        self.mode = mode
        self.created_at = time()
        self.events: list[SessionEvent] = []
        self.artifacts: list[SessionArtifact] = []
        self.result = SessionResult()
        self.emit(
            "session_started",
            task=task,
            mode=mode,
            workspace_root=self.workspace_root,
        )

    def emit(self, event_type: str, message: str = "", **data) -> SessionEvent:
        event = SessionEvent(type=event_type, message=message, data=data)
        self.events.append(event)
        return event

    def add_artifact(
        self,
        kind: str,
        title: str,
        *,
        path: str = "",
        content: str = "",
        **metadata,
    ) -> SessionArtifact:
        artifact = SessionArtifact(
            kind=kind,
            title=title,
            path=path,
            content=content,
            metadata=metadata,
        )
        self.artifacts.append(artifact)
        self.emit(
            "artifact_created",
            kind=kind,
            title=title,
            path=path,
            metadata=metadata,
        )
        return artifact

    def note_changed_file(self, path: str) -> None:
        normalized = str(Path(path))
        if normalized not in self.result.changed_files:
            self.result.changed_files.append(normalized)
        self.emit("file_changed", path=normalized)

    def note_test(self, name: str, status: str, details: str = "") -> None:
        record = {"name": name, "status": status, "details": details}
        self.result.tests_run.append(record)
        self.emit("test_recorded", name=name, status=status, details=details)

    def add_warning(self, message: str) -> None:
        self.result.warnings.append(message)
        self.emit("warning_emitted", message=message)

    def add_followup(self, message: str) -> None:
        self.result.followups.append(message)
        self.emit("followup_added", message=message)

    def complete(self, summary: str = "") -> SessionResult:
        self.result.status = "completed"
        if summary:
            self.result.summary = summary
        self.emit("session_completed", summary=self.result.summary)
        return self.result

    def fail(self, message: str) -> SessionResult:
        self.result.status = "failed"
        self.result.summary = message
        self.emit("session_failed", message=message)
        return self.result

