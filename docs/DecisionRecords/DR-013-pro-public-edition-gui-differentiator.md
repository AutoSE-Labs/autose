# DR-013: Pro vs. Public Edition — GUI Is the Only Current Differentiator

- **Status:** Superseded by DR-032 (2026-10-17)
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Product edition strategy; interaction with DR-002 (`src/` layout) and DR-003 (complexity tiers)

## Context

AutoSE ships as two editions from one codebase: a free/public edition and a paid Pro edition. What actually differs between them is a product decision with direct architectural consequences: DR-002's `src/gui`/`src/tui`/`src/logic` split, DR-003's tier set, and DR-008's stage tool registries all need an unambiguous answer to "what is gated by edition," or contributors (human or agent) risk building edition differentiation into the wrong place — most importantly, leaking it into `src/logic/`, which DR-002 requires to stay a single shared surface for every interface. A prior arrangement assumed the highest complexity tier ("Advanced" mode) was the paid differentiator, maintained via a manually-synced sibling repository. That assumption is superseded by this decision.

## Decision

For the current launch scope, the **only** edition differentiator is the desktop GUI: `src/gui/` ships in the Pro edition only. The public edition ships `src/logic/` and `src/tui/` unchanged — same classifier, same complexity tiers, same tool access, same agent/workflow behavior as Pro. There is no tier restriction, no tool-access restriction, and no model-capability restriction between editions today.

Because `src/logic/` and `src/tui/` are edition-agnostic by construction (per DR-002), packaging the two editions is a build-time inclusion/exclusion of the `src/gui` adapter against one shared codebase, not a maintained fork and not a manual porting process between divergent repositories.

A future revision may restrict capability (e.g. tool access, available tiers) in the free edition to make Pro more compelling once there is an established user base. That restriction is explicitly **not** designed or decided here: no mechanism for it is chosen, and no such restriction may be introduced through an unrelated code change — a future capability-tiering decision must supersede this DR, not quietly narrow it in passing.

## Consequences

### Positive

- No fork-sync burden for the vast majority of the code; only `src/gui` ever differs between editions.
- Keeps `src/logic/` edition-agnostic by construction, consistent with DR-002's dependency direction rather than as a separately-enforced rule.
- Leaves room to introduce capability-based tiering later without having pre-committed to today's differentiator as the permanent one.

### Negative

- Today's differentiator is a UI, not a capability — Pro's value proposition rests entirely on the GUI until (and unless) a future capability-tiering decision is made.
- That future decision, when it comes, will need its own DR and will likely touch DR-003 (tier availability per edition) and DR-008 (tool registries per edition); this DR deliberately does not pre-design that mechanism.

## Alternatives considered

### Keep the highest complexity tier ("Advanced"/`forge`) as the paid differentiator

Rejected for now, superseding the prior arrangement. Current product direction ties Pro to the GUI, not to tier access.

### Maintain two separate repositories with a manual port process

Rejected. A single shared harness with edition-scoped inclusion of `src/gui` removes the sync burden entirely for a GUI-only differentiation; a second repository would only be justified if the editions' underlying logic itself needed to diverge.

### Design and implement capability-based tiering (tool/tier limits) now

Rejected as premature. Deferred until there is a user base to justify it, per explicit current product direction; documented here as future scope rather than left undecided and undocumented.
