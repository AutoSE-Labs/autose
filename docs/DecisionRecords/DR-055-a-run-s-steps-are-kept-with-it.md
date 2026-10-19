# DR-055: A Run's Steps and Usage Are Kept With It

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/trace.py` (`TraceRecorder`, `RunTrace`); `Turn.trace` and the recorder's wiring in `Session` (`src/logic/session.py`); `TurnRecord.trace` and the trace budget in `SessionRecord.bounded` (`src/logic/persistence.py`); `SessionBridge.steps`, `GET /api/steps?session=…&turn=N`, `trace_payload` and the `has_steps` flag of a turn payload (`src/gui/`); `steps.js`, `inspectedRun()` and the picked run card in the dashboard. Builds on DR-026 (durable sessions), DR-037 (the inspector), DR-040 (diffs read on demand), DR-041 (the Usage tab). Refines DR-026's rule on what a stored session holds.

## Context

The dashboard's Steps and Usage tabs (DR-037, DR-041) were built in the
browser from the run's event stream: each tool call with its arguments, result
preview and timing; each verdict, send-back, retry and stall; each model
round's token count; each stage's start and end. Nothing on the server kept
any of it. A reload, a second tab, or any run from before the page opened
showed empty tabs, and the terminal's saved sessions had no record of what a
run had done beyond its answer and its changes.

DR-026 stores only what a session promoted and "never a raw tool transcript",
to keep DR-004's boundary on the durable copy. That rule is about what can
reach a model again. A record of the steps for a person to read is a
different thing, as long as it is bounded and never read back into a prompt.

## Decision

- **Recorded in logic, from the events.** `TraceRecorder` is an `EventSink`
  that `Session` puts beside the Adapter's own sink. It is armed when a turn
  starts and gives up a `RunTrace` when the workflow returns, raises or is
  stopped. It listens to the events the run already emits, so nothing that
  emits them changes, and every Adapter's sessions get the same record.
- **What it keeps.** Per tool call: the tool, its arguments, the stage,
  `done` / `failed` / `refused`, the elapsed milliseconds, and the result
  preview the event carried. A refused call, which never reaches the
  executor, is kept as a step with the refusal's reason. Notable moments are
  notes, each with its event type as `kind` and only the fields an Adapter
  needs to word it: classification, a stage entered again, retries, verdicts
  (passed, target, unmet), send-backs (with the unmet requirements of the
  verdict before), approvals, nudges, repeated and malformed calls, stalls,
  round caps, stops. Per model round: stage, prompt and completion tokens,
  the budget's estimate and the window. Per stage pass: start and end. The
  requirements ledger's statuses as each stage left it.
- **What it leaves out.** Stage artifacts and the validator's feedback, which
  DR-026 already keeps off disk (the route stores the shape, not the words);
  reasoning text; streamed output.
- **Bounded.** An argument value is cut at 200 characters, nested values at
  three levels and twenty items; a result preview keeps its first 400 and
  last 200 characters, since the end is where a test run says how it went.
  A turn keeps at most 200 steps and 400 rounds: past that, the first half and
  the most recent, with a count of what was dropped and where. A session file
  spends at most 2 MiB on traces, newest turn first; an older turn past that
  keeps its stage times and the counts. A stored trace is read back through
  the same caps and keeps only known fields.
- **Stored with the turn.** `Turn.trace` and `TurnRecord.trace` are optional;
  a session written before this loads with `None`, and a turn without a trace
  writes no `trace` key. `restore()` carries it; nothing reads it into
  `SessionMemory` or a prompt.
- **Served on demand.** `GET /api/steps?session=…&turn=N` returns one turn's
  steps, rounds, stage times and ledger, the latest turn without `turn`; 404
  for a turn that does not exist, 400 for one that is not a number. A turn
  payload carries only `has_steps`. As with diffs (DR-040), every turn's full
  record in each state frame would resend a long session's history on every
  change of state.
- **Dashboard.** When nothing is running, the details panel shows the run
  picked in the conversation, else the run this page watched, else the latest
  turn's stored trace. A stored trace is shaped like a watched run, so the
  Steps, Usage and Checks tabs draw it with the same renderers and the same
  wording. A click on a finished run's card picks it and marks the card; a
  second click, "Show the latest run", or a new run starting gives the panel
  back to the latest. A live run is drawn from its events exactly as before.

## Consequences

### Positive

- A run's steps and usage survive a reload, a second tab and a restart, and
  any finished run in the conversation can be inspected, not only the last.
- The terminal's saved sessions hold the same record, readable as JSON.
- One recorder for every Adapter, with no change to the workflows, the agent
  or the executor.

### Negative

- Session files grow by up to 2 MiB of traces, on top of the change content
  DR-026 already stores.
- Tool arguments and result previews now reach disk. They are trimmed, and the
  sessions directory is git-ignored, but a command line or a file's first
  lines are kept where before only the answer was.
- The dashboard words a stored note with its own copy of the live wording
  (`steps.js` beside `handleEvent`); a new note kind has to be worded in both.
- A refused call shows as a step in a stored trace but only as a note in the
  live view, which never had a step for it.
