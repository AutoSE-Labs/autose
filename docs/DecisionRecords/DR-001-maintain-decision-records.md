# DR-001: Maintain Decision Records for Notable Changes

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Project-wide documentation practice

## Context

AutoSE is under active, fast-moving development; and implementation approaches are frequently revisited as the project learns what works. This is a problem for two distinct audiences:

1. **Humans.** Contributors (including future maintainers, and the same maintainer six months later) lose the "why" behind a design once the people involved forget it or move on. Re-deriving context from diffs alone is slow and error-prone, and leads to decisions being silently re-litigated or accidentally reversed.
2. **Coding agents.** AutoSE itself is a tool for building agentic coding workflows, and this repository is also actively worked on by AI coding agents. These agents have no persistent memory across sessions beyond what is checked into the repo. Without written decision records, an agent asked to add a feature or refactor a module has to infer intent purely from existing code, which is frequently insufficient to avoid contradicting a prior deliberate choice. Decision records are, in effect, load-bearing context for the agents working on this codebase, not just historical trivia.

In short: undocumented decisions vaporize as soon as the people who made them context-switch away, and this repo's contributor base includes non-human agents that can only act on what is written down.

## Decision

Any notable decision affecting this repository must be recorded as a **Decision Record (DR)** in [docs/DecisionRecords/](../DecisionRecords/), regardless of whether it is "architecturally significant" in the strict sense. This includes, but is not limited to:
- New features or capabilities
- Architecture or module boundary decisions
- Non-trivial implementation choices where alternatives existed
- Refactors that change structure, contracts, or conventions
- Reversals or supersessions of earlier decisions
- Changes to development process/tooling that future contributors (human or agent) need to know to work effectively in this repo

The bar is deliberately low: **if a reasonable contributor could ask "why was it done this way?" and the answer isn't obvious from the code alone, it gets a DR.** Small, self-explanatory changes (a bug fix, a formatting change, a one-line config tweak) do not need one.

### Format

Each DR is a single Markdown file named `DR-NNN-short-slug.md`, numbered sequentially, in `docs/DecisionRecords/`. Each file contains:

- A title (`DR-NNN: <Decision title>`)
- Metadata: `Status`, `Date`, `Decision owners`, `Scope`
- **Context** — the problem, constraints, and why it matters now
- **Decision** — what was decided, concretely enough to act on
- **Consequences** — positive, negative, and mitigations
- **Alternatives considered** — options rejected and why

`Status` follows a simple lifecycle: `Proposed` → `Accepted` → optionally `Superseded by DR-NNN` or `Deprecated`. Superseding decisions must reference the DR(s) they replace instead of silently contradicting them.

### Process

- A DR is written alongside the change it documents (same PR/commit series where practical), not retroactively long after the fact.
- Anyone or anything (including a coding agent) making a notable decision is responsible for writing or updating the relevant DR as part of that work.
- Existing DRs are treated as binding context: a change that contradicts an `Accepted` DR must either conform to it or supersede it explicitly, not ignore it.

## Consequences

### Positive

- Design intent survives contributor turnover and long gaps between related changes.
- Coding agents working on this repo get grounded, explicit context instead of having to infer intent from code and risk contradicting past decisions.
- Disagreements about "why is it built this way" become quick lookups instead of re-litigated debates.
- Onboarding (human or agent) is faster because the reasoning behind the current structure is discoverable in one place.

### Negative

- Adds a small amount of overhead to making notable changes.
- Requires discipline to keep DRs updated (superseded/deprecated) as the system evolves, or they become misleading.

### Mitigations

- Keep the DR template lightweight; a short, precise DR is preferred over a skipped one.
- Treat an out-of-date `Accepted` DR as a bug: superseding it is part of the change that invalidates it, not a follow-up task.

## Alternatives considered

### Rely on commit messages and PR descriptions only

Rejected. These are not discoverable as a coherent decision history, are easy to lose in a large `git log`, and are not reliably available as context to a coding agent operating on a checked-out working tree.

### Only document "architecturally significant" decisions

Rejected. Restricting DRs to architecture-level decisions leaves out many implementation and process choices that are just as easy to lose and just as useful as grounding context for future contributors and agents. This repository intentionally records any notable decision, not only architecture.
