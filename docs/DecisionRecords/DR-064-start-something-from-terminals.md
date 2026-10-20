# DR-064: The Terminals Panel Starts Things, and Says Who Started Each One

- **Status:** Accepted
- **Date:** 2026-10-20
- **Decision owners:** AutoSE maintainers
- **Scope:** `BackgroundProcess.origin` and `links`, `ProcessManager.started_by` and `start(origin=…)`, `_unbuffered_environment` (`src/logic/common/processes.py`); `Session.stream` (`src/logic/session.py`); `SessionBridge.start_process`, `ask_for_process` and the `again` action (`src/gui/bridge.py`); `POST /api/processes` (`src/gui/server.py`); `request_title` (`src/logic/persistence.py`) and `session_title` (`src/gui/payloads.py`); the Terminals panel (`index.html`, `terminals.js`, `app.css`). Extends DR-062; reads DR-012 and DR-042.

## Context

DR-062 gave the dashboard a Terminals panel for what runs start in the
background, but only a run could start anything, and each row said little
beyond its command and status. People wanted to start a server themselves,
either by describing it or by typing the command, to see when each one
started, how long it has run and which session and run started it, and to get
from a terminal back to that session.

One problem showed up in use. `python3 -m http.server` printed nothing in the
panel, which said "Waiting for output…" while the server was already serving.
Its output goes to a pipe rather than a terminal, so Python held each line in a
buffer until thousands of requests had filled it.

## Decision

- **Who started it.** Each process records its origin. A run is
  `{"by": "run", "run": N, "request": …}`, where N is the run's number as the
  page counts runs; the session sets this for the length of each turn with
  `ProcessManager.started_by`. A person is `{"by": "person"}`, plus
  `again_of` when the command was run again. The origin is in the process
  payload and in `process_started`.
- **Where it serves.** The loopback addresses a command prints
  (`http://localhost…`, `127.0.0.1`, `0.0.0.0`, `[::]`) are kept, up to three,
  first seen first, with the "every address" hosts written as `localhost`.
  While the command runs, the panel offers to open each one in a new tab.
  Addresses elsewhere are not offered: a local command cannot be said to serve
  them.
- **Output as it is printed.** Background commands run with
  `PYTHONUNBUFFERED=1`, so a Python server's first line arrives when it is
  printed. Other runtimes mostly write a line at a time already. When nothing
  has arrived, the panel says the command is running and has not printed
  anything yet, rather than seeming to wait.
- **Start something, in words.** `action: "ask"` sends the person's words to
  the chosen session (the focused one by default) as an ordinary Spark run,
  queued behind a run that is already working. Their words come first,
  followed by a short note in a paragraph of its own asking for
  `run_command` with `background: true`. A session is now titled by the
  first paragraph of its first request (`request_title`, used both for the
  rail and for stored sessions), so it is named after the person's words and
  not after the note. What that run executes is asked
  about like any other call (DR-012), so describing a command is never a way
  past the approval gate.
- **Start something, as a command.** `action: "start"` runs the command
  exactly as the person typed it, in the workspace, belonging to the chosen
  session, without asking: the person wrote it, as they would in their own
  terminal. A trailing `&` is accepted and dropped. The request carries the
  dashboard token like every other write.
- **Run again** starts a finished command anew, as the person, and clears the
  old row, so each command has one row. A command still running must be
  stopped first.
- **The panel** shows a "Start something" box with an *In words* / *Command*
  switch and a session picker. Each row shows its sheet, who started it, its
  start time and how long it has run. The selected command has a row of facts:
  when it started, how long it has run or ran, who started it (with the run's
  request), its session (a link that closes the panel and goes there), its
  folder, and its process id and line count.

## Consequences

- A person can run anything the operating system lets them from the
  dashboard's Command box. That adds no new power: the same person can already
  approve any call, or switch on YOLO (DR-053). The box is still reached only
  with the dashboard's token.
- Every background command gets `PYTHONUNBUFFERED=1`. A Python program that
  relies on its output being block-buffered becomes slightly slower.
- Processes are still not saved: a restart stops them (DR-062), so their
  origin lasts only as long as the process does.
