# DR-010: Calibrate Local Context Estimates with a Safety Margin

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Request context budgeting across all tiers

## Context

Precisely counting tokens for an arbitrary OpenAI-compatible model without a provider-specific tokenizer is not reliably possible; a local heuristic estimate (e.g. character- or word-based) will systematically over- or under-count relative to the actual tokenizer in use. Using a raw, uncalibrated local estimate as if it were exact risks context-overflow failures precisely when a request is already close to the model's limit — the worst time for a budgeting mistake to surface.

## Decision

Context budgeting will treat the local token estimate as approximate and apply a calibrated safety margin before comparing it against a model's context limit, and will reserve capacity for the model's expected output *before* computing how much input budget remains, rather than only checking input size in isolation.

Exact provider-specific tokenizers are explicitly deferred rather than adopted immediately: a conservative, margin-adjusted local estimate is treated as sufficient for request-diagnostic purposes until a specific need justifies the added dependency weight of per-provider tokenizers.

## Consequences

### Positive

- Reduces context-overflow failures caused by local estimate drift, without requiring a tokenizer dependency per supported provider.
- Output-capacity reservation prevents a request that fits on input alone from still failing because there was no room left for the response.

### Negative

- A calibrated margin is inherently a heuristic; it trades away some usable input budget for headroom against estimate error.
- Still not exact — an unusually tokenizer-unfriendly prompt could still misbudget within the margin in rare cases.

## Alternatives considered

### Add exact provider-specific tokenizers now

Deferred, not rejected outright. Conservative local estimates are sufficient for current diagnostic needs; the added dependency and maintenance surface of per-provider tokenizers should be justified by a concrete failure case first.

### Use the raw local estimate with no safety margin

Rejected. This is the status quo problem being fixed — it treats an approximate count as exact and increases the chance of overflow exactly when a request is already near the limit.
