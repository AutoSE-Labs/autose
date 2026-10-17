# DR-009: Bound Agent Loops with Time Budgets and Iteration Caps

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Multi-stage workflow execution (see DR-003), runner-level enforcement. **Amended by [DR-030](DR-030-unlimited-by-default-caps-and-a-user-held-stop.md): the mechanism below stands — the bounds are still enforced by the runner and still typed, fail-closed outcomes — but every one of them now defaults to unlimited, and a user-held stop replaces the default numbers.**

## Context

A multi-stage workflow with loopback (see DR-003) needs an explicit stopping condition beyond "the verifying stage eventually says pass." Without one, a persistently failing task (a genuinely unfixable request, a model stuck repeating the same mistake, or a context/estimation failure) can loop indefinitely, consuming time and inference cost with no useful outcome and no clear signal to the user about what happened.

## Decision

The runner responsible for a multi-stage workflow enforces two independent bounds, checked at the runner level rather than left to individual stages to self-limit:

1. **A maximum validation-loopback iteration count** — a configurable cap on how many times the verifying stage may send work back before the runner stops the workflow itself, independent of what the verifying stage's own verdict says.
2. **An overall wall-clock time budget** for the request, so runtime that varies by model speed and hardware is bounded in absolute terms rather than only by iteration count.

Exhausting either bound is treated as a distinct, typed failure outcome (not an ordinary exception), and the runner does not present a task as completed/successful when either bound is hit — this is a fail-closed policy consistent with DR-005's fail-closed validation. A stage that cannot produce a valid output because of context-budget failure (see DR-010) is likewise raised as a typed stage failure rather than allowed to silently produce a degraded artifact.

## Consequences

### Positive

- Guarantees every task run terminates with a clear, typed outcome instead of a possible indefinite loop.
- Separates "ran out of time" and "ran out of attempts" from ordinary transient errors, so callers/UI can present each distinctly.
- Consistent with DR-005: neither exhaustion path can be mistaken for or silently converted into a reported success.

### Negative

- A genuinely hard task that would have converged on one more attempt still fails once the cap is hit; the cap is a deliberate tradeoff against unbounded cost, not a guarantee of eventual success.
- Requires every stage-failure path to be raised as a distinguishable, typed error rather than a generic exception, which is more implementation discipline than a bare `try/except`.

## Alternatives considered

### Rely on wall-clock time budget alone

Rejected. Runtime varies significantly by model and hardware, so time alone does not express how many failed recovery attempts are reasonable to allow; both bounds are needed.

### Let the workflow run until a human manually cancels it

Rejected as the only safeguard. Manual cancellation should remain available, but an automatic bound is required so an unattended or long-running task cannot silently consume unbounded time/cost by default.

**Amended by DR-030.** The reasoning holds for an *unattended* run and is why the bounds are kept and kept configurable. For the attended, local, interactive run AutoSE actually targets, the evidence went the other way: a cap sized for a hosted model interrupted correct work far more often than it caught a runaway loop. So the defaults are now unlimited, manual cancellation is implemented rather than merely "available" — cooperative, keeping the partial result, offered by every Adapter — and an unattended deployment is expected to set numbers in `.autose/config.yaml`.

### Treat exhaustion as an ordinary retryable error

Rejected. Conflates "worth retrying automatically" with "structurally cannot succeed without different input," and risks the caller mistaking exhaustion for a transient blip.
