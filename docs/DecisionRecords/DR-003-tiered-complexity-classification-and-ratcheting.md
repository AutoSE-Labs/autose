# DR-003: Classify Prompts into Complexity Tiers and Ratchet Upward Within a Session

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Request routing, `src/logic/` top-level dispatch

## Context

Not every user prompt needs the same amount of agentic machinery. A one-line fix and a multi-file architectural change both cost the same if every request runs the same fixed pipeline — which is what most competing coding-agent tools (Claude Code, Cursor, aider, Deepseek-engineer-style harnesses) do. That wastes latency and inference cost on small requests, and risks under-powering genuinely complex ones if the pipeline is instead kept deliberately lightweight.

The proposed approach: a single classification call assigns each prompt a complexity tier, and the session's effective tier only ever increases, never decreases, so a trivial follow-up inside an already-complex conversation still gets the full pipeline instead of silently downgrading.

## Decision

`src/logic/` will define an ordered list of complexity tiers (e.g. `spark`, `craft`, `forge`) as a single source of truth, that all routing, CLI options, and session-state code derive from rather than re-hardcoding.

- `spark` — trivial requests: a question, or a change small enough to need no write or only a near-trivial one (a single line, one value/config change, comment-level edit).
- `craft` — the everyday, most-used tier: implement a feature or fix a bug, typically touching a handful of files. Runs the baseline multi-stage pipeline (see Workflow shape below).
- `forge` — the premium tier for architecture-level work: workflow/architecture changes, changes spanning many files (tens of files), or a task that requires the system to make significant independent design decisions of its own. Extends `craft`'s pipeline with an added Design stage.

A `TaskClassifier` component will issue one low-cost LLM call per new prompt (with recent conversation history as read-only context) to select a tier, with a deterministic text-scan fallback if the model's structured response is unparsable, defaulting to the mid tier on total failure.

Session state tracks the highest tier reached so far. When a new prompt classifies to a lower tier than the session's current tier, the session's existing (higher) tier is used instead — the ratchet only moves up, never down, for the lifetime of the session.

### Workflow shape per tier

Each tier maps to a distinct workflow shape, not just a model/budget swap:

- **`spark`** — a single direct pass: answer a question, or make at most one small, self-contained edit (a single line, one value/config change, a comment). No planning stage, no requirements ledger, no multi-stage flow — the tier stays no-write or near-no-write by design.
- **`craft`** — the baseline multi-stage pipeline, shared with `forge`: **Plan** (concrete implementation plan with an IDed requirements ledger) → **Execute** (implement against the codebase, update the ledger) → **Validate** (independently check the result against the ledger and issue a fail-closed verdict). Default for ordinary feature/bug work touching a handful of files.
- **`forge`** — `craft`'s pipeline with one stage added in front: **Design** (produce an ADR-equivalent, optionally grounded in cited knowledge) → **Plan** → **Execute** → **Validate**, for architecture-level or many-file changes, or work requiring genuinely independent design decisions. A pass/fail verdict from Validate may loop back to Design (`forge` only), Plan, or Execute with targeted feedback instead of restarting from scratch, bounded by an attempt cap and time budget.

## Consequences

### Positive

- Small requests stay fast and cheap; only tasks that need it pay for heavier orchestration.
- A session that has already engaged deep, multi-stage reasoning does not lose that context/rigor because of a superficially simple follow-up message.
- Named tiers (`spark`/`craft`/`forge`) read as a spectrum of effort rather than opaque severity labels, making the active tier easier to reason about in CLI output and logs.

### Negative

- Adds one extra LLM round-trip (the classification call) before real work starts on every new prompt.
- A misclassification into a lower tier than actually needed still costs a wrong-pipeline attempt before a user notices and re-prompts (mitigated by, but not eliminated by, the ratchet).

### Mitigations

- Keep the classifier prompt and parsing deterministic/JSON-first with a conservative fallback, so failures degrade to the middle tier rather than crashing, guessing high (which would be expensive) or guessing low (under-performing).

## Alternatives considered

### Always run the same fixed pipeline for every prompt

Rejected. This is the status quo for most competing tools; it either over-spends on trivial requests or under-powers complex ones if kept lightweight, and gives the product no cost/latency lever to differentiate on.

### Let the tier be re-classified fresh on every turn with no ratchet

Rejected. A complex, multi-turn task can contain simple-looking intermediate messages ("yes, do that", "use the other approach") that read as trivial in isolation but are part of an already-complex task. Re-classifying those downward would drop the pipeline mid-flight along with the state that task still depends on — loopback position, the requirements ledger, stage-scoped tool permissions — not just switch to a cheaper model.

### Let the user manually select a tier every time

Rejected as the default. Manual override can still be supported as an escape hatch, but requiring it for every prompt reintroduces the friction automatic classification is meant to remove.
