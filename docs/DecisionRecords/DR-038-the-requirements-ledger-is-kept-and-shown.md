# DR-038: Each Turn Keeps Its Final Requirements Ledger, and the Dashboard Shows the Ledger as a Board

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** `as_payload()` in `src/logic/requirements.py`; `WorkflowOutcome.requirements` (`src/logic/workflows/base.py`) filled by `src/logic/workflows/staged.py`; `Turn.requirements` (`src/logic/session.py`); `TurnRecord.requirements` (`src/logic/persistence.py`); `turn_payload` (`src/gui/payloads.py`); the inspector's Requirements tab (`src/gui/static/inspector.js`, `app.js`, `app.css`). Builds on DR-004, DR-005, DR-026, DR-037.

## Context

DR-005 makes the requirements ledger the completion signal of a staged run:
Plan writes numbered requirements, Execute reports each one's status, Validate
verifies each independently, and the runner re-parses the verifier's ledger
rather than trusting its verdict. It is the most distinctive thing AutoSE
does, and the dashboard showed almost none of it: a one-line count on the
receipt ("4 requirement(s): 4 verified"), computed from live events, and gone
after a reload, because the ledger lived only in the request's working memory
and never reached the turn.

A person reviewing a run — or a group being shown one — wants to know which
requirements there were, how each moved through the stages, what evidence
backs each, and why Validate passed or sent work back.

## Decision

### The final ledger is part of the turn

`WorkflowOutcome` carries `requirements`: the ledger as it stood when the run
ended, as plain data (`id`, `status`, `description`, `evidence`), on every
outcome — completed, stopped, or bound-exhausted. `Turn` keeps it, the session
file stores it (DR-026), and the turn payload sends it to the dashboard.
`as_payload()` is the one place the ledger becomes plain data; `stage_finished`
uses it too.

This is a deliberate addition to what DR-004 allows into the durable copy. DR-004
keeps intermediate loop state — transcripts, per-stage artifacts, tool output —
out of it. The final ledger is not intermediate: like `files_changed`, it is a
small, canonical statement of what the turn produced. The test that pins the
stored turn's fields now lists it, with that reasoning beside it.

The field is optional on read: a session written before it existed has none,
which is not a format change and needs no version bump.

### The board

The inspector's **Requirements** tab shows, for the run the page watched: a
count by status; the validator's verdict (and the unmet IDs and feedback when it
did not pass); each loop-back; and each requirement with its status, its journey
across the stages (Plan · Not Started → Execute · Implemented → Validate ·
Verified), and its evidence. For a turn finished before the page loaded it shows
the stored final ledger, without the journey, which only the live events carry.

## Consequences

### Positive

- What "done" meant for a run, and why the run counts as done, is visible and
  survives a reload and a restart.
- A failed validation shows which requirement failed and what the validator
  said, next to the evidence for the ones that passed.

### Negative

- The per-stage journey is not stored; a reloaded page shows only the final
  statuses for turns it did not watch.
- Every stored staged turn grows by its ledger: a few hundred bytes for a
  typical one.

### Mitigations

- The journey is rebuilt from the replayed timeline for a running turn
  (DR-033), so a reload mid-run loses nothing.

## Alternatives considered

### Parse the ledger back out of the stored artifacts in the browser

Rejected. The artifacts are not stored (DR-004), the ledger is stripped from
them, and a second parser of the ledger format in JavaScript could drift from
the one the runner trusts.

### Store every stage's ledger, not only the final one

Rejected for now. It is intermediate state of the kind DR-004 keeps out of the
durable copy, and the final ledger answers the question a reviewer asks.
