# DR-061: Undo One File of One Run

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `Session.undo_file` in `src/logic/session.py`; `CHANGED_SINCE` in `src/logic/common/changes.py`; `SessionBridge.undo_file` and the `undone` list of `SessionBridge.changes` in `src/gui/bridge.py`; `POST /api/undo` in `src/gui/server.py`; "Undo this file" in `src/gui/static/` (diff viewer, Changes tab, a finished run's files table). **Extends DR-024 and DR-040.** The whole-run undo is unchanged.

## Context

DR-024 journals every file a run writes and lets the person undo the last run
that wrote: every file it changed is put back, except one that has changed
since, which is left alone and named. DR-040 lets the Changes tab read any
run's diff, file by file.

Reviewing file by file and undoing run by run do not fit together. A run that
fixed a bug in one file and made an unwanted edit to another can only be kept
whole or thrown away whole; keeping the fix means undoing the run and asking
for the fix again, or putting the other file back by hand. And only the last
run that wrote can be undone at all, though the person may be looking at an
earlier one.

## Decision

- **One file of any run can be put back.** `Session.undo_file(turn, path)`
  restores the file's content from before that run: an edited file gets its
  old content, a file the run created is removed, a file it deleted comes
  back. The run's other files are not touched.
- **The same check as the whole-run undo.** The file must still hold exactly
  what that run wrote. If it does not, nothing is written and the reason is
  given. A later run whose record still holds a change to the file comes
  first, and the reason names it ("run 3 changed it afterwards; undo run 3's
  change to it first"), since undoing that change first is what makes this
  one possible. That holds even when the file's bytes match this run's again
  (a later run wrote them back): putting the file back then would throw the
  later run's change away while its record still claimed it. Otherwise the
  reason is the general "it has changed since AutoSE wrote it", which covers
  the person's own edits. A change whose content was not kept (too large, or
  trimmed from a stored session by `SessionRecord.bounded`) is refused with
  the reason it carries.
- **Recorded as a whole-run undo records it.** The change moves from the
  run's `changes` to its `undone` list, the session is saved, and
  `changes_reverted` is emitted with the one path and the run's index. The
  whole-run undo reverts `changes`, so it passes a file already undone this
  way by; a run whose files have all been undone one by one is no longer the
  one it undoes.
- **One endpoint.** `POST /api/undo` takes an optional `turn` and `path`. With
  neither it undoes the last run that wrote, exactly as before. With both it
  undoes that file: a file that cannot be put back is a 409 with the reason,
  a run or file that is not there a 404, and one without the other a 400. It
  is refused while any session is running, as the whole-run undo is, because
  the workspace is shared.
- **`/api/changes` lists a run's undone files** beside its changes, without
  diffs, so the diff viewer can show them struck through instead of dropping
  them from the run.
- **On the page**, "Undo this file" sits in the diff viewer's file head, in the
  Changes tab's file rows (in place of the line counts while the row is
  hovered, focused or open) and in a finished run's files table (shown on
  hover and focus). It asks first, naming the file and the run, and says what
  will happen: put back, deleted or brought back. Afterwards the file shows as
  undone everywhere and the counts leave it out.

## Consequences

### Positive

- A run can be kept in part: the person keeps the files they want and puts
  the others back, without asking the model again or editing by hand.
- Any run's file can be undone, not only the last run's, while the check
  that nothing is overwritten stays the one DR-024 made.
- A refusal says what to do next when a later run is in the way.

### Negative

- Files are put back one at a time; there is no way to pick several files of
  a run and undo them together, short of undoing the whole last run.
- A file that a later run also changed can only be undone in order, newest
  first, one file at a time.
- The terminal UI still offers only `/undo` of the last run that wrote.
