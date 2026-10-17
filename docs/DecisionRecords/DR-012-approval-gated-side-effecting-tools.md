# DR-012: Approval-Gated Execution for Side-Effecting and Network Tools

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Tool execution across all clients (TUI, GUI, headless), all tiers

## Context

Some tools are not like `read_file` or `search_files`: `run_command` executes an arbitrary shell command, and `web_search`/`fetch_url` reach outside the workspace onto the network and pull back untrusted external content. DR-008 decides which stage/tier *owns* which tool, but says nothing about whether an individual *call* to one of those tools is allowed to execute without a human noticing. Left to each client to decide independently, this is exactly the kind of cross-cutting safety property that is easy to implement inconsistently — or silently skip — in a new interface, since nothing forces every adapter to enforce it the same way. An unreviewed shell command or an unreviewed network fetch is a real risk surface (arbitrary command execution, exposure to untrusted fetched content, unbounded network egress from a user's machine), not merely a UX preference.

## Decision

A single, shared set of **approval-required tools** — currently `run_command`, `web_search`, and `fetch_url` — is defined once in the shared agent/tool layer and read by every client, never re-declared or re-decided per adapter.

Before a call to any tool in that set is executed:

1. The client must surface the concrete call — the tool name and its actual arguments (the exact shell command; the exact query/URL) — to the user, not a generic "a tool wants to run" notice.
2. Execution blocks until the user approves or denies that specific call.
3. A denied call never reaches the underlying tool implementation; the model instead receives an explicit denial string as the tool result, so it can adapt rather than silently retry.
4. The request and its resolution are both recorded as auditable events (e.g. `approval_requested` / `approval_resolved`), so a session's event log shows what was asked and what was decided, independent of whether the session transcript itself is kept.

An **explicit, opt-in** auto-approve mode may exist for unattended use (e.g. headless/CI invocation), but it must be a deliberate flag set by the invoking user/operator for that run — never a default, and never inferred from context. Approval-required status is a property of the tool's identity, enforced in the shared layer every client goes through, the same enforcement posture DR-008 uses for tool ownership — not a convention documented only in a prompt.

## Consequences

### Positive

- One trust boundary regardless of which interface (TUI, GUI, headless) issued the call — a new adapter inherits the gate automatically instead of having to reimplement it.
- Produces an auditable approval trail for every side-effecting or network call.
- Unattended automation remains possible without weakening the default posture, since auto-approval requires explicit, deliberate opt-in.

### Negative

- Adds a synchronous human-in-the-loop step that slows down fully automated runs unless auto-approve is deliberately enabled for that context.
- A careless blanket auto-approval configuration (e.g. left on by default in a CI template) reintroduces the exact risk this decision exists to prevent; the safety property depends on operators treating that flag deliberately, not on the mechanism alone.

## Alternatives considered

### Auto-approve a "safe" subset by heuristic (e.g. read-like commands)

Rejected for now. Classifying a shell command as "safe" is unreliable — a destructive command can be trivially disguised as a read-like one — and the added classification logic is itself a new failure surface. Revisit only with a concrete, narrow case.

### Enforce approval only via prompt instructions

Rejected. As with DR-008's tool-permission enforcement, telling the model to "ask before running commands" is guidance, not enforcement, and can be steered around; the gate must live in code every client goes through, not in prose the model reads.

### Leave approval gating as a per-adapter concern

Rejected as the status quo risk. Without one shared, mandatory definition, a new or existing adapter can omit the gate entirely, by oversight rather than by decision.
