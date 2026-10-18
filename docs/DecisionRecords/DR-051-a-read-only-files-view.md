# DR-051: A Read-Only Files View in the Dashboard

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/common/browse.py` (`list_workspace`, `read_workspace_file`); `Bridge.files` and `Bridge.file_text` in `src/gui/bridge.py`; `GET /api/files` and `GET /api/file?path=`; new `src/gui/static/files.js` and the file links it puts on commands, steps and Needs you.

## Context

A run reads and writes files the person cannot see from the dashboard: a
command waiting for approval names `requirements.txt`, a step says "Writing
app.py", and checking either meant leaving for an editor. People asked to
browse the workspace and to open a named file in place, and to follow a file
while a run writes it.

## Decision

- The dashboard gets a **Files** view: the workspace as a tree, files opened
  in tabs with line numbers and light colouring, and **Watch**, which re-reads
  the open file every three seconds and marks the lines that changed.
- It is **read-only**. Nothing in it writes, renames or deletes.
- Reads go through `Workspace.resolve`, the boundary every tool uses: a path
  outside the root, or a symlink whose target is outside it, is refused (403).
  The folders the tools never walk (`.git`, `.venv`, `node_modules`, caches,
  `.autose`) are neither listed nor read. A file is shown up to 1 MB; a binary
  file is reported, not shown. A listing stops at 5,000 files.
- A file name that exists in the workspace becomes a link wherever the page
  shows a command or a step, and opens in the view.

## Consequences

### Positive

- What a run touches can be read where the run is watched.

### Negative

- Anything in the workspace is readable by whoever holds the dashboard's
  token, `.env` files included. That was already true of the model's tools,
  and the dashboard serves only localhost; it is still a wider reader.
- Watch compares line by line, so an inserted line marks every line after it.
