# ADR-001: Shared Core with First-Class CLI and Desktop Clients

- Status: Accepted
- Date: 2026-07-05
- Deciders: AutoSE maintainers
- Technical Story: Evolve AutoSE from a TUI-centric application into a multi-client agent product while preserving the CLI as a first-class user surface.

## Context

AutoSE currently has a packaged CLI entrypoint, but that entrypoint effectively boots the current terminal UI and routes execution through TUI-owned session state and rendering flow.

At the time of this decision:

- `src/autose/cli.py` delegates to `code/logic/main.py`.
- `code/logic/main.py` imports and runs `code/tui/session.py`.
- `code/tui/session.py` owns prompt input, classification, tier dispatch, approval handshakes, and live rendering updates.
- `code/tui/standard_tui.py` executes the standard Plan -> Code -> Test workflow against `TUIState`.

This shape is acceptable for the current prototype, but it creates three strategic problems:

1. Product coupling
   The current product behavior is coupled to the terminal UI. Any desktop application would either need to wrap terminal behavior or duplicate orchestration logic.

2. Client asymmetry
   The CLI cannot evolve independently as a polished product surface because it is tightly bound to a specific rendering and interaction model.

3. Abstraction mismatch
   The desired AutoSE product is task-oriented, closer to a Codex-style agent experience where the default surface is task, progress, approvals, and outcome. Code, diffs, ADRs, logs, and test output are artifacts that should be shown when relevant, not the primary interface by default.

The team wants to keep the CLI as a real product, not a debug or fallback surface. At the same time, the team wants to support a desktop experience that abstracts away implementation details unless the user explicitly needs them.

The team also wants to avoid:

- meaningful runtime slowdowns,
- duplicated logic across CLI and desktop,
- large long-term delivery overhead,
- a refactor that makes the system harder to iterate on.

## Decision

AutoSE will adopt a shared-core, multi-client architecture.

The core decision is:

- extract a headless `autose-core` execution layer from the current TUI-centric flow,
- preserve the CLI as a first-class product client,
- add the desktop application as another first-class product client,
- make both clients consume the same task/session/event/artifact model,
- treat code, diffs, ADRs, logs, and test output as structured artifacts rather than UI-owned state.

The CLI will remain a user-facing product surface.

The desktop application will not wrap terminal output and will not scrape CLI text. It will communicate with the shared core over a structured boundary.

## Decision Drivers

- Preserve the CLI as a high-quality terminal-native product.
- Enable a higher-abstraction desktop application without duplicating agent logic.
- Keep orchestration, tool execution, and artifact generation implemented once.
- Avoid coupling core behavior to one rendering model.
- Support progressive disclosure of technical detail.
- Minimize runtime overhead by keeping the core close to the existing direct execution path.
- Reduce long-term delivery cost relative to maintaining separate application stacks.

## Considered Options

### Option 1: Continue growing the current TUI as the product core

Description:
Keep the existing architecture and extend the terminal UI until it can also support a desktop wrapper or desktop-adjacent behavior.

Pros:

- Lowest short-term refactor cost.
- Keeps current implementation momentum.
- No immediate boundary design required.

Cons:

- Keeps business logic coupled to TUI state and rendering concerns.
- Makes desktop support awkward and likely brittle.
- Encourages output scraping or dual implementations.
- Makes the CLI harder to evolve as a clean, first-class product.
- Increases long-term maintenance cost.

Decision:
Rejected.

### Option 2: Desktop-first rewrite, CLI becomes secondary

Description:
Define the desktop application as the primary product and keep the CLI only for development, power users, or compatibility.

Pros:

- Simplifies product focus if desktop is the only strategic surface.
- Allows desktop-specific UX decisions without compromise.

Cons:

- Conflicts with the requirement that the CLI remain a real product.
- Alienates terminal-native usage patterns that are likely important for AutoSE users.
- Risks splitting the implementation between desktop assumptions and CLI expectations.

Decision:
Rejected.

### Option 3: Shared core with first-class CLI and desktop clients

Description:
Extract a headless engine and standardize a task/session/event/artifact contract that multiple clients can consume.

Pros:

- Preserves the CLI as a product.
- Makes desktop integration clean.
- Centralizes orchestration and tool logic.
- Supports progressive disclosure of detail.
- Aligns with the intended task-first product shape.
- Keeps long-term feature work additive instead of duplicated.

Cons:

- Requires an upfront refactor.
- Demands disciplined boundary design.
- Introduces some migration complexity while both old and new paths coexist.

Decision:
Accepted.

## Chosen Architecture

### 1. Shared Core

AutoSE will introduce a headless core responsible for:

- task intake,
- context loading,
- mode or workflow selection,
- orchestration of planning, coding, testing, and design/ADR generation,
- tool execution coordination,
- approval requests,
- artifact generation,
- structured final results.

This core must not depend on terminal rendering classes or desktop UI code.

### 2. First-Class CLI Client

The CLI will become a client of the shared core, not a thin alias for the TUI.

The CLI remains a first-class experience with:

- rich streaming output,
- approvals,
- progress visualization,
- artifact inspection,
- transparent access to code and logs when desired.

The CLI may remain more verbose and transparent than the desktop application by default, but it must still consume the same underlying execution model.

### 3. Desktop Client

The desktop application will be another client of the shared core.

Its default UX should be task-oriented:

- task input,
- current status,
- progress,
- approvals,
- final summary,
- expandable artifacts.

The desktop application should avoid showing raw implementation detail unless needed. Code, diffs, plans, ADRs, test logs, and command logs should be progressively disclosed.

### 4. Structured Boundary

The stable boundary between core and clients will be defined around:

- session objects,
- event streams,
- artifact records,
- final result payloads,
- approval requests and responses.

This boundary is the primary architectural contract introduced by this ADR.

## Required Domain Model

### Session

A session represents one user task execution lifecycle. A session should include:

- session id,
- task text,
- workspace root,
- selected mode or workflow,
- timestamps,
- current status,
- approval state,
- artifact index,
- final result summary.

### Events

Events communicate incremental execution progress. Likely event categories include:

- `session_started`
- `context_loaded`
- `task_classified`
- `plan_started`
- `plan_chunk`
- `approval_requested`
- `approval_resolved`
- `code_started`
- `file_changed`
- `test_started`
- `test_chunk`
- `adr_generated`
- `warning_emitted`
- `session_completed`
- `session_failed`

Event payloads must be structured and machine-readable. Clients may render them differently, but they must not parse human-facing text to recover semantics.

### Artifacts

Artifacts are durable outputs produced during a session. Likely artifact categories include:

- plan,
- diff,
- changed file summary,
- ADR,
- command log,
- test result,
- validation result,
- final explanation.

Artifacts should be referenceable independently from how any given client chooses to display them.

### Final Result

Each completed session should produce a structured final result containing at least:

- user-facing summary,
- status,
- changed files,
- tests run,
- generated artifacts,
- follow-up items,
- escalation or attention flags if applicable.

## Key Architectural Rules

1. No client may own business logic that determines core execution behavior.
2. No client may rely on scraping terminal output for execution state.
3. TUI-specific state containers must not remain the source of truth for orchestration.
4. Artifact generation must happen in the core or in core-owned services.
5. The CLI and desktop application must share the same semantic event model.
6. Direct execution paths should remain available so the CLI does not pay unnecessary transport overhead.
7. The architecture must allow progressive disclosure: summaries first, details on demand.

## Performance and Developer Productivity Implications

### Runtime Performance

This change is expected to preserve practical runtime performance.

Reasoning:

- The dominant cost in AutoSE is model inference, tool calls, subprocesses, file I/O, repo inspection, and validation work.
- UI rendering is not the primary bottleneck.
- A shared core does not materially change the cost of planning, coding, testing, or artifact generation.

To preserve performance:

- the CLI should call core functionality in-process where practical,
- the desktop client may use an out-of-process boundary if needed, but the protocol must stay lightweight,
- event streaming must remain incremental rather than requiring heavy state snapshots on each update.

### Development Velocity

This change introduces short-term refactor cost but improves long-term velocity.

Expected short-term cost:

- extracting TUI-owned workflow logic,
- defining structured events and artifacts,
- migrating existing execution paths.

Expected long-term gain:

- one implementation of orchestration,
- one artifact model,
- one approval model,
- feature additions made once and surfaced in multiple clients,
- lower risk of client drift or duplicate bugs.

This ADR explicitly rejects over-engineering. The boundary should be as small as possible while still separating core execution semantics from presentation.

## Consequences

### Positive Consequences

- CLI remains a first-class product.
- Desktop can be built cleanly without terminal emulation.
- AutoSE becomes task-first rather than TUI-first.
- ADRs, plans, diffs, logs, and tests become reusable artifacts.
- Future clients, including possible web or remote clients, become possible without major rework.

### Negative Consequences

- Refactor complexity increases in the near term.
- Some current code paths will need temporary adapters during migration.
- Existing TUI assumptions may surface hidden coupling that must be unwound.

### Neutral Consequences

- The final desktop delivery technology remains a separate decision.
- This ADR does not require a specific desktop framework, only a structured client/core boundary.

## Non-Goals

This ADR does not decide:

- the final desktop framework choice,
- the exact packaging strategy for desktop delivery,
- whether execution is fully local or partially remote in future versions,
- the final visual design of either the CLI or desktop application.

This ADR also does not require a full rewrite. It requires incremental extraction of stable boundaries from the current implementation.

## Migration Plan

### Phase 1: Define the Contract

- Define session, event, artifact, and final-result schemas.
- Identify the minimum set of semantics currently embedded in `TUIState`.
- Identify which current messages are presentation and which are actual execution events.

### Phase 2: Extract Core Workflow Execution

- Move plan/code/test orchestration into core-owned services.
- Replace direct dependence on TUI rendering state with structured event emission.
- Preserve current behavior through adapters where needed.

### Phase 3: Rebuild the CLI on Top of Core

- Make the CLI a product client of the new core.
- Preserve rich streaming output and approvals.
- Ensure the CLI can inspect artifacts without owning core workflow logic.

### Phase 4: Add Desktop Client

- Build a task-oriented desktop shell on the same core contract.
- Default to summary/progress/approval/result views.
- Expose artifacts progressively.

### Phase 5: Remove Legacy Coupling

- Delete obsolete TUI-owned orchestration once parity is reached.
- Keep only presentation-specific client code in the CLI/TUI layer.

## Risks and Mitigations

### Risk: Boundary becomes too abstract and slows delivery

Mitigation:

- Keep the first schema small.
- Model only what current workflows actually need.
- Avoid designing for speculative clients or distributed systems before they are required.

### Risk: CLI experience regresses during migration

Mitigation:

- Treat the CLI as a first-class acceptance target.
- Preserve streaming and approvals during each migration stage.
- Validate parity on narrow flows before broadening scope.

### Risk: Desktop requirements pressure the core into UI-specific compromises

Mitigation:

- Make the core own semantics, not visuals.
- Keep client rendering decisions outside the execution layer.

### Risk: Duplicate temporary paths linger too long

Mitigation:

- Make legacy TUI orchestration explicitly transitional.
- Delete adapters once core parity is achieved.

## Acceptance Criteria

This ADR is considered implemented when:

1. A task can execute through a headless core without requiring TUI-owned state.
2. The CLI runs as a first-class client of that core.
3. Core execution emits structured events and artifacts.
4. Code, diff, test, and ADR outputs are available as artifacts rather than only as rendered terminal content.
5. A desktop client can consume the same execution model without parsing CLI text.

## Follow-On Decisions

The following decisions should be captured separately after this ADR:

- desktop framework selection,
- local process model for the desktop client,
- artifact persistence policy,
- approval and permission model hardening,
- remote or multi-workspace session support, if added later.
