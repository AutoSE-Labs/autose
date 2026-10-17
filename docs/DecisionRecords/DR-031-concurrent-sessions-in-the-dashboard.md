# DR-031: The Dashboard Holds Several Sessions at Once, Each With Its Own Turn

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/gui/bridge.py`, `src/gui/server.py`, `src/gui/static/`. **No change to `src/logic/` or `src/tui/`.** Builds on DR-029 (the dashboard is a local web page) and DR-030 (the user holds the stop).

## Context

`SessionBridge` held exactly one `logic.session.Session`, and every request
that would touch it was gated on one process-wide `_running` flag. A turn ran
on a worker thread — that part was already right, and the HTTP server was
already threaded — but the flag meant that for as long as the turn lasted, the
dashboard refused:

- opening a new session, and resuming or switching to a stored one;
- clearing history, changing tier or model, and `undo`;
- and, in the page itself, *clicking anything in the rail at all*: the click
  handler returned early while `state.running`, the New session button was
  disabled by it, and a CSS rule greyed every row out.

The reasoning behind the gate was sound as far as it went. `Session` is not
concurrent: it owns a `ChatModel`, a rolling memory, a tier ratchet and a
cancel token, and two turns inside one of those is a race, not a feature. But
"one turn per session" was implemented as "one turn per dashboard", and those
are not the same claim. The result is the complaint that prompted this record:
a staged `forge` run against a local 7B model takes minutes, DR-030 removed the
caps that used to end it early, and for the whole of that time the window was a
progress display. The user could watch, or stop. Nothing else.

That is worse here than it would be in a hosted tool, for two reasons specific
to AutoSE. Local inference is slow enough that a run is a genuine wait rather
than a pause. And a session is cheap — an object, a memory buffer, and a
connection to a runtime that is already on the machine — so the thing being
rationed cost almost nothing to have twice.

## Decision

**One turn per session; as many sessions as the desk holds, each generating
independently.** The bridge stops being *a session* and becomes *the open
sessions*.

### 1. A desk of live sessions, keyed by id

`SessionBridge` holds `dict[str, _Live]` in the order sessions were opened.
A `_Live` is the turn state that used to be bridge-wide — `running`, the
current prompt, the streaming answer buffer, and the parked approvals — now
owned per session. One of them is *focused*: the session the conversation
column is drawing. Focus is server-side, like everything else here, because
two tabs on one dashboard have always shown one shared view.

`MAX_OPEN_SESSIONS` is 6. Each open session that runs is a worker thread and a
model connection, and past a handful they only slow each other down; closing
one loses nothing, since it is on disk and reopens from the rail (DR-026).

### 2. Every frame says which session it came from

The event sink and approval gate are built *before* the `Session` and closed
over the `_Live` that will hold it, so `_publish_for` can tag each frame with
its session id. Frames about the dashboard rather than one session carry
`"session": ""` explicitly rather than omitting the field.

This is load-bearing, not bookkeeping: with two turns streaming, an untagged
`answer` frame would append one session's tokens to the other's card. The page
keeps one live card per session id, updates all of them from the stream, and
paints only the focused one — which is what makes switching back to a run in
progress instant instead of a refetch that has lost the activity feed.

### 3. What a busy session blocks, and what it does not

Gating moves from "is anything running" to "is *this* session running":

- **Never refused, whatever is running:** opening, resuming, focusing and
  reading a session; reading changes, knowledge and diagnostics; and answering
  an approval. This is the substance of the change.
- **Refused for that session only:** a second prompt into it (409 — the
  `Session` race the old flag was really protecting against), and changing its
  tier, model, or history while it works.
- **Refused while *any* session is running:** `undo`, and a config reload.

Those last two are the honest cost of the concurrency and are stated as
properties rather than left to luck. `undo` rewrites files on disk; the
workspace is shared and each turn journals its own writes (DR-024), so putting
one session's files back while another run is editing them would destroy work
nobody asked to lose, and neither journal can see the other. A reload rebuilds
every session against a possibly different runtime, model or workspace, so a
session still holding the old connection would be a lie about what it runs
against — every open session is closed and one fresh one is opened.

A second run starting in a workspace where one is already going publishes a
`concurrent` frame, and the page says once what it costs: two journals, so
`Undo` only covers the session it is run from.

### 4. A closed session is not an abandoned thread

Closing a *running* session is refused with a reason ("stop it first") rather
than dropping it: its worker would carry on writing files with nothing on
screen accounting for it. Closing the last one opens a fresh session, so the
dashboard is never left with nothing to type into. Deleting a stored session
that is open is likewise refused before the file goes.

### 5. Approvals reach the browser without seizing it

An approval for the focused session takes the overlay, as before. One for a
session the user is not looking at is announced — a badge on its rail row and
one toast naming it — and left parked. Stealing the window to ask about a run
happening elsewhere would undo the entire point of this record. DR-012 is
unchanged and still decides the outcome: unanswered is denied, at the same
timeout, per session.

### 6. The page stops telling the user to wait

The rail is live while a turn runs: rows show a breathing dot, the elapsed
clock, and a badge when that session needs a decision. New session is disabled
only when the desk is full. The composer's hint, and the refusal when a second
prompt is typed into a working session, now name the way out — open another
session — instead of only offering Stop.

## Consequences

### Positive

- The window is usable while a long run generates, which is the whole request.
  Start a `forge` run, open a second session, ask a `spark` question in it,
  read a diff, browse stored work — none of it waits.
- Nothing in `src/logic/` changed. The concurrency is an Adapter property, and
  `Session` stayed exactly as unconcurrent as it was, which is what made this a
  contained change rather than a rewrite (DR-002).
- Per-session frame tagging fixes a latent bug the single-session shape hid: a
  second tab starting a turn used to feed its events into the first tab's card.
- The terminal Adapter is unaffected and keeps one session, which is the right
  shape for a single-window interface.

### Negative

- **Two runs contend for one local runtime.** Ollama serving a 7B model to two
  sessions is slower at both than at either alone. This is not mitigated in
  code — it is the user's call, and a run they started while another was going
  is a run they chose to start.
- **Two runs share one workspace.** They can edit the same file, and neither
  journal sees the other's writes, so `undo` is per session. Blocking `undo`
  while anything runs prevents the worst version of this; it does not make
  concurrent edits safe, and nothing short of workspace-per-session would.
- More concurrent state to reason about. Six sessions is six worker threads,
  six model connections, and a state payload that carries a row per session.
- The state frame's shape grew an `open` array and a `focus` field, and every
  write endpoint grew an optional `session`. Existing readers are unaffected —
  the focused session is still flattened into the top level — but there are now
  two ways to ask about a session.

### Mitigations

- The `concurrent` frame says, once, what sharing a workspace costs, rather
  than leaving the user to discover it from an `undo` that missed a file.
- `undo` and reload name the sessions they are waiting on, with short ids, so
  the refusal is actionable rather than just a 409.
- The desk is capped at 6 and the rail shows what is running, so "why is
  everything slow" has an answer on screen.
- `_live_payload` is deliberately not a session's history: six transcripts in
  every state frame would put a megabyte on the socket each time any one of
  them finished a turn. Focusing a session is what sends its history.

## Alternatives considered

### Keep one session, but stop the page from freezing

Rejected as the fix to the symptom rather than the cause. The rail could be
made clickable and the buttons enabled, but every one of those actions still
has to replace the one `Session` the running turn is using — so they would
either be refused on arrival, or corrupt the run. The freeze was a truthful
rendering of a real limitation; the limitation is what had to go.

### One run at a time globally, with the UI free to navigate

The conservative version: switch sessions, read history and diffs, open a new
session, but submitting is refused until the current run finishes. It removes
the model contention and the concurrent-write hazard entirely, and it was
seriously considered. Rejected because it leaves the actual ask half-done — the
user wanted to *be useful* during a long run, and "you may look around but not
work" is still a wait, just a better-decorated one.

### Queue submissions and run them one at a time

Rejected as the worst of both. It adds a scheduler, a queue state per session,
and a new set of questions (what happens to a queued prompt when its session is
closed, or the tier changes, or the queue is reordered) in exchange for
removing a contention cost the user is better placed to judge than a policy is.
If model contention turns out to matter more than expected, this is the
extension to reach for — and it composes with the desk rather than replacing
it.

### A process per session

Rejected. It buys real isolation — separate memory, no shared journal, a crash
that takes one session down — at the cost of every problem process supervision
brings: lifecycle, orphans, IPC for the event stream, and a shared workspace
that is still shared. The isolation that actually matters here is the workspace
one, and a subprocess does not provide it.

### Per-session workspaces (a worktree each)

Tempting, and the only thing that would make concurrent edits genuinely safe.
Rejected for now as a much larger decision than this one: it changes what a
session *is*, needs a git dependency AutoSE does not have, and forces a merge
story on a tool whose users are already in a repository they manage themselves.
Recorded here as the natural successor if concurrent editing becomes a real
problem rather than a documented cost.
