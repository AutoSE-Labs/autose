# DR-026: A Session Is Durable and Resumable, Stored as One File Per Session

- **Status:** Accepted
- **Date:** 2026-08-30
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/persistence.py`, `Session`, the CLI's `--resume`/`--sessions`. **Implements the durable half of DR-004's `SessionMemory`; carries DR-024's change record.**

## Context

A `Session` held everything it knew in memory: the completed turns, the
rolling history `SessionMemory` bounds, the tier the session had ratcheted to
(DR-003), the token tally, and — since DR-024 — the content needed to undo
what it wrote. Closing the terminal discarded all of it.

For the hardware DR-015 targets that is expensive in a way it is not for a
hosted agent. A single `forge` request against a local model can run for an
hour; a `craft` session that establishes context over several turns represents
real elapsed time and real tokens. "Start again from the beginning" is a poor
answer to a closed laptop lid, a dropped SSH connection, or a machine that
went to sleep.

It also left DR-024 half-delivered. Undo works because a `FileChange` holds
both halves of the change and the journal is reconstructible from the record —
but if the record dies with the process, the ability to undo dies with it, and
the window in which a user can review and revert is exactly one terminal
session.

`.autose/` already exists as the git-ignored directory AutoSE owns for
DR-011's tool-output artifacts, so there is a natural home and no new
convention to invent.

## Decision

A session is written to disk after every turn, and can be picked up again.

### One JSON file per session, under `.autose/sessions/`

`FileSessionStore` writes `<id>.json`. One file per session rather than one
index, so a corrupt or unreadable session costs that session and nothing else
— the listing skips it and reports why, rather than failing wholesale. Writes
go to a temporary file and are renamed into place, so an interrupted save
leaves the previous version intact instead of a truncated one.

Ids are `YYYYMMDD-HHMMSS-<random>`: time-ordered so a directory listing is
chronological and "the latest session" needs no index, with a random suffix so
two sessions started in the same second cannot overwrite each other. An id is
reduced to alphanumerics, `-` and `_` before it becomes a filename, so nothing
derived from one can escape the sessions directory.

The directory gets a `.gitignore` containing `*` on first write. A transcript
of someone's work is theirs, and must not arrive in a commit because AutoSE
put it in a tracked directory — the same posture DR-011 takes.

### An explicit format version

Every record carries `version`. A record naming a version this build does not
understand is **refused with an explanation** rather than parsed
optimistically, so a session written by a newer AutoSE reads as "upgrade, or
start a new session" instead of silently losing whatever field was added.

### Only what a session already promoted is stored

The record holds the same distilled `(prompt, answer)` pairs `SessionMemory`
holds, the rolling summary, the turn list, and the change record. Never a raw
tool transcript, never an intermediate stage artifact. DR-004's boundary
applies to the durable copy exactly as it applies to the live one — a test
asserts on the stored turn's exact field set, so a future field cannot be
added to storage without someone deciding to.

### Persistence never breaks a run

Every failure path degrades: an unwritable directory, a truncated file, a
record from the future, a store that raises. Each records a problem an Adapter
can surface and lets the run continue. `Session.persist()` catches everything
and emits `session_save_failed`. A session that works is never sacrificed to a
session that might be resumed.

### The tier ratchets on resume

`restore()` puts the stored tier through DR-003's ratchet rather than
assigning it, so resuming a `forge` session cannot silently drop to `spark`,
and a session already higher than the record is not pulled down.

### Stored change content is capped, and says when it hit the cap

256 KiB per file, 8 MiB per session. A change past a cap keeps its path and
its place in the history and loses its content, marked unrestorable with the
reason — so a resumed session says "this file changed, and I can no longer put
it back" rather than either lying about it or carrying a 40 MB blob in a JSON
file. The session budget is spent newest-turn-first, because the recent turns
are the ones anyone would actually undo.

### The surface

`autose --resume` picks up the most recent session for this workspace;
`autose --resume <id>` takes an id or an unambiguous prefix of one.
`autose --sessions` lists them and exits, and `/sessions` does the same from
inside a running session.

## Consequences

### Positive

- An hour of `forge` work survives a closed terminal.
- DR-024's undo window extends past the process that created it.
- A session's history is inspectable as plain JSON, which makes "what did it
  actually do" answerable without a running AutoSE.
- Per-workspace storage means resuming in a repository offers that
  repository's sessions, not a global pile.

### Negative

- Prompts and answers now persist to disk by default. That is the point, but
  it is a real change in what AutoSE leaves behind, and a user working on
  something sensitive should know it.
- A session's stored size grows with its change content, bounded but not
  small.
- Resuming restores history, not the workspace. If files moved since, a
  restored session's undo will decline to act — correctly, but the mismatch
  can be surprising.

### Mitigations

- The sessions directory is git-ignored on creation.
- Sessions are pruned to the most recent 50, so the directory does not grow
  without limit.
- A refused undo names its reason per file (DR-024), which is what makes the
  stale-workspace case legible rather than mysterious.

## Alternatives considered

### One index file listing every session

Rejected. A single file is a single point of corruption, and every save
rewrites the whole history rather than one session. Per-file storage makes a
damaged session cost exactly that session.

### SQLite

Rejected for now. It buys concurrent-write safety AutoSE does not currently
need — sessions are per-process and per-workspace — at the cost of a schema,
migrations, and a binary blob a user cannot read. JSON keeps the stored
session inspectable, which is worth more here than transactional writes.

### Store sessions globally, under `~/.config/autose/`

Rejected. Sessions are about a workspace, and a global pile makes "resume what
I was doing in this repository" a search problem. It would also put one
repository's prompts somewhere a user cleaning up that repository would not
look.

### Save continuously rather than per turn

Rejected. A turn is the unit that gets promoted to session memory (DR-004), so
it is the only point at which the session has a consistent state worth
writing. Mid-turn saves would store states the session model does not admit.

### Replay the full stored history into the model on resume

Rejected. It ignores the bounds DR-004 exists to enforce and would blow the
context window of exactly the local models DR-015 targets. `restore()` re-bounds
what it loads, so a record written under a larger cap cannot smuggle a longer
history in.

### Do not store change content, only paths

Rejected. It would leave undo working only within the session that made the
change, which is the limitation this decision exists to remove, and would make
DR-024's guarantee quietly conditional on nothing having restarted.
