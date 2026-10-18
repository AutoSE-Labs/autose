# DR-045: A Person Can Review the Plan Before Anything Is Changed

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/workflows/review.py` (`PlanReviewGate`, `PlanReview`, `PlanDecision`); `WorkflowRequest.plan_review`; the review step and `rescope` adoption in `src/logic/workflows/staged.py`; `Session.review_plans_with`; the bridge's gate, `POST /api/plan` and `POST /api/plan-review`; the plan card and the "Review plan first" switch in the dashboard. Builds on DR-005 (the requirements ledger), DR-012 (the gate pattern), DR-030 (the user holds the stop).

## Context

Craft and Forge write a plan with a requirements ledger before touching any
file (DR-005), and then go straight to Execute. The plan is the cheapest point
to correct a run: nothing has been changed, and a wrong approach caught here
costs one stage instead of three plus an undo. The dashboard showed the plan
only as it streamed past, and offered no way to stop on it, change it, or
answer it. The old Tauri desktop app had a plan review; the rebuild dropped it.

## Decision

### A gate, in logic

`PlanReviewGate` is a Strategy in the shape of `ApprovalGate`: after the Plan
stage succeeds, the staged runner asks it, showing the plan with its ledger.
The answers:

- **approve** — on to Execute;
- **revise** with feedback — Plan runs again with the reviewer's words;
- **edit** with a replacement plan — adopted through the same checks as the
  model's own plan; if it fails them (no valid ledger, say) the reviewer is
  asked again with the reason;
- **stop** — the run ends as a stop does (DR-030), with nothing changed.

With no gate attached — the default for the terminal, the JSON adapter and
evaluation harnesses — nothing waits and behaviour is unchanged.

### The reviewer may change the scope; the model may not

DR-005 refuses a stage output whose requirement IDs differ from the ones it was
given, so a model cannot quietly drop or invent requirements. That rule is about
the model. A plan the person edited, or asked to have rewritten, may add or
remove requirements: the ledger from before is not a contract they made. A
re-plan the validator asks for still keeps the IDs.

### In the dashboard

A "Review plan first" switch per session attaches the bridge's gate. The waiting
plan is a card in the conversation — "nothing has been changed yet" — with
Approve and continue, Ask for changes, Edit plan and Stop; it appears under
"Needs you" (DR-044), raises a notification (DR-043) and counts in the tab
title. A plan waits without a timeout, unlike an approval: nothing has been
changed, so waiting costs nothing, and Stop ends the wait.

## Consequences

### Positive

- A wrong approach is corrected before any file is touched.
- The person can tighten or widen what "done" means, in the ledger itself.

### Negative

- A review that nobody answers holds the run indefinitely.
- An edited plan is plain text; a malformed ledger is only caught when it is
  submitted.

### Mitigations

- Review is off by default and per session; the card says what it is waiting
  for, and Stop always ends it.
- A refused edit comes back with the parser's reason, and the plan is unchanged.

## Alternatives considered

### Review in the adapter only, by stopping and restarting the turn

Rejected. It would discard the stage's state and run Plan from scratch, and
every adapter would reimplement it.

### Approve the plan automatically after a timeout

Rejected. Running on after silence is the opposite of what a review is for.
