# DR-008: Stage-Isolated, Least-Privilege Tool Registries

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Tool ownership and filesystem mutation boundaries across agent stages

## Context

A common shortcut in agent tooling is one shared, mutable tool registry with one broad permission set (read, write, execute) handed to every agent, and, in multi-stage workflows, a runner that temporarily patches shared tool handlers to observe one stage's behavior and restores them afterward. This creates real bugs: two concurrent runs can corrupt each other's tool state, a failed restoration can leave a later agent wired to the wrong callback, and every stage ends up with broader permissions than its actual responsibility requires (e.g. a review/validation stage that can also write files).

## Decision

Each agent instance (per stage, per tier) owns its own cloned tool registry rather than holding a reference to a shared global one. Module-level tool definitions remain immutable; an agent constructor clones the definitions it needs into its own instance, so a runner that needs to instrument or override a specific handler (e.g. to capture file diffs) does so only on that instance's cloned registry, with no restoration step required afterward since the clone is discarded with the agent.

Stage permissions are enforced in the handler code itself, not only implied by tool descriptions in the prompt:

- A design/decision-record-producing stage may create or edit decision-record files only inside the recognized decision-records directory.
- A planning/review stage is read-only.
- An implementation stage may write/edit within the workspace, subject to existing workspace-escape checks.
- A validation stage is read-only and has no write/edit tool in its schema at all; it reports missing coverage or gaps and routes them back to the implementation stage rather than fixing them itself.

No new tool types or a general plugin mechanism are introduced by this decision — it changes ownership and permission enforcement of the existing tool set only.

## Consequences

### Positive

- Concurrent runs cannot corrupt each other's tool callbacks; no shared global mutable state to race on.
- Tool ownership and permissions become deterministic and directly testable per stage.
- A validation stage that cannot write remains an actually independent quality gate rather than one that could paper over its own findings.

### Negative

- A validation stage can no longer add a missing test itself — it must loop back to the implementation stage instead (an intentional tradeoff, not an oversight).
- Every agent instance constructs a small number of short-lived tool objects rather than reusing shared ones — negligible runtime cost, minor code volume increase.

## Alternatives considered

### Protect the shared global registry with a lock

Rejected. Serializes runs and still leaves ownership ambiguous; per-instance cloned registries remove the shared mutable state entirely instead of just serializing access to it.

### Deep-copy entire tool registries per agent

Rejected. Tool definitions may contain callables; only the dataclass/schema container needs cloning, not a deep copy of everything reachable from it.

### Give every stage the full tool set and rely on prompt instructions alone to restrict behavior

Rejected. Prompt-level restriction is guidance, not enforcement; a stage that should never be able to write files must be structurally unable to, not merely told not to.
