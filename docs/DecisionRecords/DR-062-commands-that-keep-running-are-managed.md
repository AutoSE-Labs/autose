# DR-062: Commands That Keep Running Are Started, Watched and Stopped by AutoSE

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/processes.py` (`ProcessManager`, `background_form`, `stop_managers`, `exit_cleanly_on_termination`); `run_command`'s `background` argument and the new `process_output` / `stop_process` tools (`src/logic/common/tools/shell.py`, `background.py`, `__init__.py`); `run_command` allowances (`offer_for` in `allowances.py`); `Session.processes` and `Session.close`; `WorkflowRequest.processes` and the spark and staged registries; the executor's reserved arguments; `SessionBridge.processes` / `process_action`, `GET`/`POST /api/processes` and the Terminals panel (`terminals.js`); the TUI's `/terminals` and its exit path. Refines DR-012, DR-048, DR-050 and DR-052.

## Context

`run_command` runs a command to completion with its output captured. A command
that starts something meant to keep running — `python server.py &`,
`npm run dev &`, `nohup … &` — never completes in that sense: the child it
leaves behind holds the output pipe open, so the call waited out its whole
timeout (two minutes by default), and the process was then orphaned. Nobody
could see what it printed, and nothing stopped it when the session, the
dashboard or the terminal app ended. Models start dev servers all the time,
usually to check a page or an endpoint, and every one of them cost a hung
round and a stray process.

## Decision

- **A process manager per session.** A command meant to keep running starts
  in its own process group (its own session) inside the workspace, with stdin
  closed and stdout and stderr combined. The last 2,000 lines of its output
  are kept, readable again from a cursor; a line still being written (a
  prompt, a progress bar) is shown but not counted. Its status is running,
  exited with a code, or stopped. Stopping sends SIGTERM to the whole group
  and SIGKILL after two seconds. One session may have eight running at once,
  so a model that starts its server again every round cannot fill the
  machine. A command that exits but leaves something running in its group
  that still holds its output (`./start.sh` that runs `node server.js &`)
  still counts as running: it can be stopped, and it cannot be cleared from
  the list until it has been, or nothing would reach what it left behind.
- **What counts as "meant to keep running".** A command ending in a lone `&`
  (optionally starting with `nohup`), or any command called with the new
  `background: true` argument. Both, because the model already writes `&`
  and should not have to learn a flag for it to work, and because the flag
  says it without shell syntax. The manager runs the command *without* the
  trailing `&` and the `nohup`: it provides both itself. Only a trailing `&`
  counts — `a & b` is still run as before. In `a; b &` the whole line goes to
  the background, not only `b`.
- **`run_command` returns at once.** It starts the command through the
  manager, watches it for about two seconds (long enough for a server to say
  where it listens, or for a typo to fail), and returns the process id,
  whether it is still running, and its first lines. The workspace checks run
  first, on the command as written. Without a manager (a caller with no
  session) such a command is refused rather than left to hang. A plain
  command behaves exactly as before.
- **Two new tools, beside `run_command` wherever it is.** `process_output`
  reads a process's latest lines (or lists them all); `stop_process` stops
  one. Neither needs approval: reading output runs nothing, and stopping can
  only reach a process this session started under a `run_command` call that
  was itself approved — it undoes something already allowed. Neither takes
  anything that could name another process on the machine; the manager is
  injected by the executor, and a model-supplied `processes` argument is
  dropped like `workspace` is.
- **Approval is unchanged, and allowances are no wider.** Starting a command
  in the background is still a gated `run_command` call, shown verbatim. An
  allowance judges the command without its trailing `&` and leading `nohup`
  — exactly what the manager runs — so `npm run dev &` is no longer
  once-only, but is covered only by `npm run dev`. Everything else DR-048,
  DR-050 and DR-052 refuse is still refused (`rm … &`, a redirect, a `&` in
  the middle), and a rule written by hand names the command without the `&`.
- **Nothing outlives what shows it.** `Session.close` stops everything the
  session started. The dashboard calls it when a session is closed, when a
  config reload or an unattended run replaces sessions, and on shutdown; the
  terminal app and the headless runner call it on the way out. While they
  run, SIGTERM and SIGHUP become an ordinary exit so those paths run even on a
  `kill` or a closed terminal window, and an `atexit` hook stops whatever is
  left after any other normal exit.
- **Events, not output, on the sink.** The manager emits `process_started`,
  `process_exited` and `process_cleared` through the session's event sink.
  Output itself is read on demand from the cursor: a chatty server as events
  would flood every Adapter's log and the dashboard's frame history.
- **The dashboard shows them all.** Processes belong to the workspace, so
  Terminals is a global section in the rail, with a badge counting what is
  running. It lists every open session's processes and shows the selected
  one's output, read from its cursor once a second while the window is open,
  in a dark box that follows the end until the person scrolls up. Stop and
  Clear (for a finished one) go through `POST /api/processes`. A step that
  went to the background says so and links to its terminal. The TUI has
  `/terminals` to list, show and stop them.

## Consequences

### Positive

- A dev server no longer hangs a round for two minutes; the model gets its
  first output in about two seconds and can read more or stop it later.
- Nothing a run starts is left running unseen: it is listed, readable and
  stoppable, and it ends with the session that started it.
- Allowing `npm run dev` once covers starting it in the background too,
  without covering anything else.

### Negative

- Output only reaches the manager through the pipe; a command that writes to
  a file instead (`… > log &`) shows nothing in its terminal, and such a
  redirect stays once-only.
- A background command started inside a longer line (`server & sleep 2; curl
  …`) is not recognised and still hangs until the timeout; only a trailing
  `&` or the `background` argument is.
- `process_output` changes as the process prints, so it is not treated as a
  read-only call by the stall checks of DR-030: a model that polls it forever
  is not stopped for stalling, only by the person or its other bounds.
- A process that double-forks into a new session of its own escapes its
  group and is not reached by a stop. So does one a command leaves running
  with its output sent elsewhere (`server > log &` inside a script) once the
  command itself has ended: nothing holds the pipe, so it is not known to be
  there.
- Killing AutoSE with SIGKILL, or a crash in the interpreter, still leaves
  processes running; only an orderly exit can stop them.
