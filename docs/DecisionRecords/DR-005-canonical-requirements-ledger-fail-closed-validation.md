# DR-005: Canonical Requirements Ledger and Fail-Closed Validation

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Multi-stage (`craft` and `forge` tier) workflow handoff and completion criteria

## Context

In any multi-stage agent workflow (plan → implement → verify), the final "this is done" signal is only as trustworthy as what the verifying stage actually checked. A verifying stage that free-form asserts "looks good" against a plan it may not remember precisely, with no stable identifiers for what was promised, cannot be trusted as a real quality gate — and most current coding-agent tools stop at exactly this level of rigor: the model says it's done, and that's treated as fact.

This is addressed with a deterministic, machine-parsed requirements ledger shared across stages, rather than another free-form status string or an additional LLM extraction call.

## Decision

The planning stage of any multi-stage workflow must emit a small, strictly formatted `REQUIREMENTS STATUS` section listing uniquely IDed requirements (e.g. `R1`, `R2`, …) with a description.

Every downstream stage that touches those requirements (an implementation stage, then a verification stage) must echo back **exactly the same IDs** — never inventing, dropping, splitting, or renaming them — updating only the status value and, for the implementation stage, attaching evidence (files/tests touched) per requirement.

`src/logic/` parses this section with deterministic code (no additional LLM call) into immutable requirement records that become canonical request-scoped state (see DR-004's `RequestMemory`), replacing any prior free-text/stale status field.

The workflow's runner enforces fail-closed completion: a verification stage's "passed" verdict is **not accepted at face value**. The runner independently parses the verification stage's ledger and overrides a claimed pass into a loopback (with the specific unmet requirement IDs surfaced as feedback) if any requirement is missing, unverified, or marked failed. A structurally invalid ledger (bad IDs, wrong format) is itself treated as a stage failure, not silently ignored, and blocks that stage's output from becoming canonical state.

## Consequences

### Positive

- Makes "is this actually done" objectively checkable instead of trusting the model's self-report.
- Loopback feedback becomes specific and actionable ("R3 is unverified") instead of vague.
- No added inference cost or external storage — parsing is deterministic.
- Closes a trust gap that is currently unaddressed in most competing agent harnesses.

### Negative

- Requires every stage to follow a strict, small markdown contract; a formatting slip fails the stage rather than being tolerated.
- The verifying stage can still issue a legitimate loopback even when every requirement is verified (e.g. for quality/architecture concerns outside the ledger's scope) — the ledger is necessary but not sufficient for a pass.

## Alternatives considered

### Trust the verifying stage's free-form verdict

Rejected. This is the status quo for most agent harnesses and provides no mechanism to catch a confidently wrong "done" claim.

### Use an additional LLM call to extract/verify requirement completion

Rejected. Adds latency, cost, and its own failure surface for something a deterministic parser handles reliably given a fixed format.

### Build a general-purpose requirements database

Rejected as unnecessary scope for a single workflow run; an in-memory, request-scoped ledger is sufficient.
