# DR-056: A Session's Queue Is Saved With It and Comes Back Held

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `QueuedRequest` and the optional `queue` field of `SessionRecord` in `src/logic/persistence.py`; `Session.keep_queue`, `Session.queued` and the save lock in `src/logic/session.py`; `_keep_queue`, `_restore_queue` and the `reopened` flag of the queue payload in `src/gui/bridge.py`; the `queued` count in `record_payload` (`src/gui/payloads.py`); the held-queue wording and the closed-session row in `src/gui/static/app.js`; the session list in `src/tui/render.py`. **Amends DR-042**, whose queue lived only in memory, and extends DR-026's record.

## Context

DR-042 gave each open session in the dashboard a queue: the next request can be
sent while a run works, and it starts when that run ends. It listed its own
weakness: the queue was in memory. Restarting the dashboard, a crash, or closing
the session lost every request still waiting, though the session itself
survived on disk (DR-026). On a local model a queue can hold an afternoon of
work, and losing it silently is worse than never having had it: the person
believes the next steps are lined up.

## Decision

### The queue is part of the stored session

`SessionRecord` gains an optional `queue`: each waiting request's id, prompt,
tier and when it was queued. It is written whenever the queue changes (a
request is added, moved, removed, cleared, started, or put back after a refused
start), so what is on disk is never behind what is on screen. A record without
the field loads with an empty queue; the format version does not change.

The queue still lives in the bridge, which still decides when the next request
starts (DR-042). The `Session` only holds the list as plain data so its own
snapshot carries it: the bridge hands the list over with `keep_queue`, which
saves at once, and every later save of the session (after a turn, on a rename)
keeps it. A terminal session resumed from the same file keeps the queue
untouched and lists it ("2 queued in the dashboard").

A queued request is the person's own words, like a turn's prompt, so storing it
does not cross DR-004's boundary.

### Saves of one session are serialised

The bridge saves a changed queue from the request's thread while a turn may be
saving on its worker thread. Two writes of one session file at once can leave
neither on disk (both use the same temporary file). `Session.persist` therefore
takes a lock, so the two saves happen one after the other. The bridge saves the
queue with its own lock held, so changes reach the file in the order they were
made.

### Reopened, the queue is held

When a session with a saved queue is opened again, from the rail or as the
session the dashboard reopens at start (`--resume`), its queue comes back
**held**: nothing starts by itself. It shows as "Paused: N up next" with Resume,
and is listed under Needs you like any held queue. The page says why it waits:
"saved from last time", not "the last run stopped", which
may not be true. Resume runs the requests in order, as before.

Held rather than resumed because after a restart nobody has said "go": the
requests were queued against a run that is no longer happening, perhaps hours
ago, and DR-030's rule is that the person holds the stop.

### Closing keeps the queue

Closing a session on purpose keeps its saved queue; it comes back held when the
session is reopened. A closed session's row in the session list leads with the
count ("2 queued") so the person knows to reopen it, and the close button's
tooltip says the queue stays. Dropping the requests is done in the queue
(remove, or clear), not by closing. Deleting the session deletes its queue with
it.

## Consequences

### Positive

- A restart, a crash or closing a session no longer loses the requests lined up
  in it.
- A reopened queue never runs unexpectedly, and the page says why it is waiting.
- Two saves of one session can no longer interleave, which also covers a rename
  during a run.

### Negative

- The request a run was working on when the dashboard stopped is not in the
  queue any more and not yet a turn: it is lost, as DR-026 already says of a
  turn in progress.
- Every queue change rewrites the session file, change content included.
- A closed session is still pruned past the store's cap of 50 (DR-026), queue
  and all.

### Mitigations

- The queue is short and changes only when the person acts, so the extra writes
  are few; the per-session content caps keep each one bounded.
- The closed session's row shows its queue, so a forgotten one is visible
  before it ages out.

## Alternatives considered

### Store the queue in its own file, kept by the bridge

Rejected. The session's own saves would not know about it, two files can
disagree after a crash, and deleting or pruning a session would leave an
orphan behind. One record per session is DR-026's rule.

### Start a reopened queue by itself

Rejected. After a restart nobody is necessarily watching, and the requests may
no longer make sense; the person resumes it.

### Clear the queue when a session is closed

Rejected. Closing is how a full desk makes room (DR-031's limit of six open
sessions); it should not throw work away. The queue has its own controls for
that.
