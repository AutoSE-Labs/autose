# DR-049: A Tier a Person Chooses Is Used as Chosen; the Ratchet Is for Classification

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `Session.choose_tier` and `Session.request_tier` in `src/logic/session.py`; the bridge's `request_tier`; the terminal's `/tier`. Narrows DR-003's ratchet to automatic classification.

## Context

DR-003 lets a session's tier only move up, so a follow-up like "yes, do that"
inside a complex task is not classified as spark and dropped out of its
pipeline. The same ratchet was applied to a tier the person picked. After one
craft run, choosing spark in the dashboard or with `/tier spark` silently stayed
on craft, and there was no way back short of a new session. The person reported
it as a bug, and it is one: the ratchet protects against the classifier
misreading a message, and a person picking a tier is not that.

## Decision

- `request_tier` keeps the ratchet and is used only for classification.
- `choose_tier` sets the tier the person picked, higher or lower. The
  dashboard's picker, the terminal's `/tier` and a tier named with a request
  all use it.
- Auto classification after a manual choice still ratchets from the chosen
  tier.

## Consequences

### Positive

- The tier picker always does what it shows.

### Negative

- A person can drop a complex task to spark mid-way and lose its pipeline.

### Mitigations

- That only happens on an explicit choice, which is shown in the picker and
  announced as a tier change.

## Alternatives considered

### Ask for confirmation before lowering

Rejected. The picker is already a deliberate act; a second question for it is
the kind of prompt people click through.
