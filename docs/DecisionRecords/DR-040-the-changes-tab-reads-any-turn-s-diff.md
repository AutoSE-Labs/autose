# DR-040: The Inspector's Changes Tab Lists Every Turn's Writes and Reads Any Turn's Diff on Demand

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** `SessionBridge.changes(turn=…)` and `GET /api/changes?turn=N` (`src/gui/bridge.py`, `src/gui/server.py`); the inspector's Changes tab (`src/gui/static/inspector.js`, `app.js`, `app.css`). Builds on DR-024 (journalled, revertible changes), DR-026 (durable sessions), DR-037 (the inspector).

## Context

DR-024 records every file AutoSE writes with the content on both sides, which
is what makes a change reviewable as a diff and revertible. The dashboard used
little of it: a "What it wrote" popup showing the **last** turn's diffs, opened
from the turn's receipt. A session with several writing turns had no way to read
an earlier turn's diff, and nothing showed the session's changes together.
Reviewers of Kiro Crew single out exactly this: the activity is agent-shaped,
and what actually changed on disk is hard to see.

Every turn in the snapshot already lists its changes as metadata (path, kind,
lines added and removed). What was missing was a place to show them and a way
to fetch an older turn's diffs.

## Decision

- `GET /api/changes` takes an optional `turn` (an index into the session's
  history) and returns that turn's changes with diffs; without it, the last
  turn's, as before. An index outside the history is a 404; one that is not a
  number, a 400.
- Diffs stay out of the state snapshot (DR-024's reasoning: a diff is fetched
  when someone wants to read it). The Changes tab fetches a turn's diffs the
  first time one of its files is opened and keeps them for the page's life.
- The tab lists turns newest first, each with its files and line counts, a total
  for the session, and the running turn's edits so far. A file expands to its
  diff inline.
- **Undo** appears only on the most recent turn that wrote, because that is what
  `undo` reverts (DR-024); it shares one action with the diff popup.

## Consequences

### Positive

- Every change the session made is in one place, readable as a diff, including
  turns from before a restart (the changes are in the session file, DR-026).
- No new data is sent with each state frame.

### Negative

- Undo is still the latest writing turn only; an older turn's changes can be
  read but not reverted from here.
- A diff fetched once is not refetched if the session is undone from another
  tab; the cache is cleared on this page's own undo.

### Mitigations

- Reverting an arbitrary turn or file needs the journal to check that later
  turns did not touch the same file, which belongs in `logic/` with its own
  record; this tab is where it would surface.

## Alternatives considered

### Send every turn's diffs with the state

Rejected. It would put whole diffs of every turn on the socket with every state
frame, for a panel most frames are not drawing.

### A separate page for changes

Rejected. The inspector already sits beside the conversation for exactly this
kind of record (DR-037), and a second page would split the session's account.
