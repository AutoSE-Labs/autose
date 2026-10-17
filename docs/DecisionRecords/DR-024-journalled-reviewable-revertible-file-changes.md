# DR-024: File Changes Are Journalled, Reviewable, and Revertible — Not Gated by Default

- **Status:** Accepted
- **Date:** 2026-08-30
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/changes.py`, the mutating tool handlers, `Session`, and every Adapter. **Extends DR-012, uses DR-008's instrumentation seam.**

## Context

DR-012 gates the tools that reach *outside* the workspace: `run_command`
executes arbitrary shell, `web_search` and `fetch_url` reach onto the network.
Each is shown to the user with its actual arguments and waits for a decision.

The tools that rewrite the workspace *itself* had no equivalent. `write_file`
and `edit_file` applied their changes and returned a one-line confirmation —
"Edited 2 block(s) in src/thing.py" — and that was the entire record. Three
consequences followed:

- **The change was unreviewable.** A user was told which files were touched
  and nothing about what was done to them. Reading the result meant opening
  each file and reconstructing the change from memory.
- **The change was irreversible.** Undoing a turn meant `git checkout`, which
  helps only if the work was committed first, and takes the user's own
  uncommitted edits with it if it was not.
- **`spark` reported nothing at all.** Change tracking lived in the staged
  runner (`_track_changes` in `workflows/staged.py`), so the one tier with an
  edit tool and no stages was also the one tier that never said what it had
  written.

Meanwhile the README's summary of the product read "Nothing side-effecting
runs unreviewed", which was true of shell and network and not true of the
thing AutoSE does most.

The obvious fix — add `write_file` and `edit_file` to
`APPROVAL_REQUIRED_TOOLS` — is worse than it looks for the models DR-015
targets. A local model implementing a plan makes many small edits; a prompt
per edit is a prompt people learn to answer without reading, which converts a
real gate into a reflex and degrades the shell and network prompts alongside
it. Approval fatigue is not a UX complaint here, it is a security regression.

## Decision

Workspace changes are **recorded, shown, and revertible** rather than gated.
A stricter posture is available to a workspace that wants it, and is not the
default.

### A `ChangeJournal` records both halves of every accepted write

`logic/common/changes.py` owns a `ChangeJournal`, which wraps a mutating
handler through DR-008's instrumentation seam — the agent's *own* cloned
registry, so the wrapper dies with the agent and needs no restoration step. It
snapshots the target file before the call and after it, and keeps the pair as
a `FileChange` holding `before`/`after` as **bytes**, so a file that is not
valid UTF-8 is still restorable exactly.

Composition, not replacement, is what makes this safe to apply everywhere: the
design stage's decision-records confinement (DR-018) and each tier's edit cap
still run underneath, and only their *accepted* writes are recorded. A refused
write returns an `Error:` string, which is how a refusal is distinguished from
a change.

### Per file, first-before and last-after

Five edits to one file coalesce into one change whose `before` is what the
file said when the turn started and whose `after` is its current content. Undo
therefore restores the original rather than the fourth intermediate state, and
the diff shows the net effect rather than a replay. A file edited back to
exactly where it started leaves no change at all.

### Undo verifies before it writes

`revert()` compares each file's current content against what the journal last
wrote. If they differ, the file has been touched by someone else — the user, a
formatter, a later turn — and it is **skipped and reported**, never
overwritten. Restoring a stale snapshot over a human's edit would destroy work
AutoSE never made, which is a strictly worse failure than declining to undo.
A partial revert keeps what it could not restore, so a second attempt retries
only those.

### The journal is per turn, and the change record travels on the turn

`Session` creates one journal per prompt and carries the resulting
`FileChange` tuple on the completed `Turn`. Because a `FileChange` holds both
halves, the object that can revert it is *reconstructible from the record*
(`ChangeJournal.from_changes`) rather than having to be kept alive — which is
what lets `/undo` work after the run that produced it has ended, and what will
let a persisted session (DR-026) restore the ability to undo.

### Gating edits is a workspace opt-in

`ApprovalPolicy` replaces the module-level `requires_approval()` check inside
the executor. It is a *value* handed to each run rather than a global, so a
workspace that sets `approval.require_edit_approval: true` gets
`write_file`/`edit_file` added to its gate without that stricter posture
leaking into another session in the same process. When edits are gated, the
approval panel renders the proposed replacement — approving a write means
approving its content, and a prompt showing only a path is unanswerable.

### Every tier reports what it changed

Tracking moved from the staged runner into the shared `track_changes` helper,
which `spark` now uses too. The staged runner keeps populating
`RequestMemory.files_changed` — the ledger and the stage prompts read it —
by passing `note_file_changed` as an observer alongside the journal.

## Consequences

### Positive

- A change is reviewable as a diff (`/diff`), summarised with line counts
  after every turn, and revertible in one word (`/undo`).
- `spark` finally reports its edits, closing a gap where the fastest tier was
  also the least transparent.
- Undo works on an uncommitted working tree, which is exactly the state a user
  running an agent is usually in — and never clobbers their own edits.
- Binary and non-UTF-8 files survive a revert byte for byte.
- Making the gate a per-run value removes a global that two concurrent
  sessions could disagree about.

### Negative

- The journal holds file contents in memory for the length of a session, so a
  long session touching many large files costs real memory. Bounded, but a
  real cost that did not exist before.
- Undo is not a general history: it reverts the most recent turn that wrote,
  not an arbitrary point.
- Adding `path` to the approval summary keys means an approval-required call
  with no command, URL, or query is now described by its path rather than by
  its full argument JSON. That is an improvement for edits and a change in
  behaviour for anything else that lands there.
- The default remains that a file write happens without being asked about
  first. That is a deliberate trade, and it is the part of this decision most
  worth revisiting if evidence appears that users want the interruption.

### Mitigations

- Snapshots are capped (2 MiB per file, 500 files per turn). A file past the
  cap is still *reported* as changed and explicitly marked not restorable,
  rather than being silently held or silently dropped — the failure is visible
  in the summary as `unrecorded`.
- A skipped revert names the specific reason per file, so "it has changed
  since AutoSE wrote it" is distinguishable from "it was too large to keep".

## Alternatives considered

### Add `write_file`/`edit_file` to `APPROVAL_REQUIRED_TOOLS`

Rejected as the default, and implemented as an opt-in. Against a local model
making twenty small edits to implement a plan, a prompt per edit trains the
user to press `y` without reading, which degrades the shell and network
prompts that genuinely need attention. Available for a workspace whose risk
posture warrants it.

### Rely on git for review and undo

Rejected. It works only when the tree was clean and committed beforehand, it
cannot distinguish AutoSE's edits from the user's own uncommitted work, and
`git checkout` would discard both. AutoSE's read-only git tools (DR-025) make
git *inspectable*; they do not make it a safety net. A workspace that is not a
repository at all — a real case for a local-first tool — has no fallback.

### Keep only the paths, and show a diff by re-reading the file at render time

Rejected. The "before" no longer exists on disk by then; there is nothing to
diff against. It would also make the rendered diff depend on when it was
rendered.

### Snapshot the whole workspace before each turn

Rejected. The cost scales with repository size rather than with what was
actually changed, which is precisely backwards, and it would copy build
artifacts and virtual environments to protect a two-line edit.

### Store snapshots on disk under `.autose/` rather than in memory

Rejected for now, and the natural extension if memory pressure proves real.
It would also survive a crash, which the in-memory journal does not. Deferred
rather than dismissed: doing it well means deciding on retention, pruning, and
what a stale snapshot directory means, which is more decision than this change
needs.

### Let undo overwrite a file that changed since AutoSE wrote it

Rejected outright. The whole value of an undo people trust is that it cannot
destroy work it did not create. A forced variant was considered and left out:
a user who wants that outcome can revert the file themselves, having seen the
diff.
