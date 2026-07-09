from __future__ import annotations

from .session import TaskSessionRecorder


def get_recorder(state: object) -> TaskSessionRecorder | None:
    return getattr(state, "task_session", None)


def emit_event(state: object, event_type: str, message: str = "", **data) -> None:
    recorder = get_recorder(state)
    if recorder is not None:
        recorder.emit(event_type, message=message, **data)


def add_artifact(
    state: object,
    kind: str,
    title: str,
    *,
    path: str = "",
    content: str = "",
    **metadata,
) -> None:
    recorder = get_recorder(state)
    if recorder is not None:
        recorder.add_artifact(
            kind,
            title,
            path=path,
            content=content,
            **metadata,
        )


def note_changed_file(state: object, path: str) -> None:
    recorder = get_recorder(state)
    if recorder is not None:
        recorder.note_changed_file(path)


def note_test(state: object, name: str, status: str, details: str = "") -> None:
    recorder = get_recorder(state)
    if recorder is not None:
        recorder.note_test(name, status, details)


def complete_session(state: object, summary: str = "") -> None:
    recorder = get_recorder(state)
    if recorder is not None:
        recorder.complete(summary=summary)


def fail_session(state: object, message: str) -> None:
    recorder = get_recorder(state)
    if recorder is not None:
        recorder.fail(message)
