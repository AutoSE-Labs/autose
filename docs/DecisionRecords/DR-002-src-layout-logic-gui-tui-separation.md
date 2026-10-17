# DR-002: `src/` Layout — Logic Owns Computation, GUI/TUI Are Thin Wrappers

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Top-level `src/` folder structure and module boundaries

## Context

AutoSE will be built as a real product with more than one way for a user to interact with it, and further interfaces are expected to be added over time. All interfaces must drive the same underlying computation. This decision is written before any interface or computation code exists, so the boundary between them is fixed as a starting constraint the first commit must already respect, rather than something inferred or retrofitted after logic and presentation code have already intermingled.

Supporting multiple interfaces on shared computation requires an explicit, enforced boundary between "what computes the result" and "what displays it." Without one, business logic tends to leak into presentation code, or gets duplicated across interfaces instead of shared. The repository is organized around a `src/` layout with three top-level folders:

```text
src/
  gui/
  logic/
  tui/
```

This DR fixes the intended responsibility of each folder, and the patterns used to keep them decoupled, before any of them contain code — so every contributor, human or agent, writes their first line into the right place under a boundary that was chosen on purpose, not one discovered by accident later.

The boundary is also a composability goal, not just a tidiness rule: the codebase must be **plug-and-play**, able to grow by adding new self-contained pieces that snap onto a stable interface, rather than by editing pieces that already exist — a "lego" model of construction. That property has to be designed in from the first commit; it cannot be bolted on afterward without a rewrite, which is why it is decided here rather than left to emerge.

## Decision

### `src/logic/` owns all computation

All business/computation logic lives under `src/logic/`. This is the only place decisions get made and work gets done; it must be fully usable with no UI attached.

`src/logic/` is subfoldered by domain/concern as those domains emerge, grouped by what the code *decides or computes*, never by which interface happens to call it.

### `src/tui/` and `src/gui/` are thin wrappers only

`src/tui/` and `src/gui/` contain only rendering/display code, input handling that translates user interaction into calls against `logic/`'s public entry points, and wiring/bootstrapping. They must not contain business logic. Any decision more complex than "how do I display this" or "which logic entry point do I call" belongs in `src/logic/`, exposed through a clear interface both wrappers can call identically.

`src/tui/` and `src/gui/` are independent, swappable frontends over the same `src/logic/` surface. Neither may import from the other, and neither may bypass `logic/`'s public interface to reach internal modules directly.

### Dependency direction

Dependencies point one way only: `tui/` and `gui/` depend on `logic/`; `logic/` never imports from `tui/` or `gui/`. This is the enforceable test for "is this actually logic or actually presentation."

### Plug-and-play composition: Adapter and Strategy, not ad hoc wiring

Two patterns are the mandated mechanism for keeping the codebase plug-and-play, and are to be used from the first code written in each layer, not retrofitted once a second case appears:

**Adapter**, at the `tui`/`gui` boundary. Every interface is an Adapter over `src/logic/`'s public entry points: it translates its own environment's input/output into calls against a fixed `logic/` interface, and translates results back into that environment's display primitives. A new interface is added by writing one new Adapter, never by changing `logic/` or an existing frontend.

**Strategy**, inside `src/logic/`, wherever more than one interchangeable way of doing something could exist behind one call shape. Any point in `logic/` where behavior could reasonably vary — by configuration, by runtime decision, or by future extension — must be expressed as an interface with one or more interchangeable implementations behind it, selected by whatever calls it, rather than as a single hardcoded implementation or a conditional branch that grows with each new case.

The test for whether something qualifies as a Strategy seam: could a second, materially different implementation be added by writing one new class/module and registering it, with zero edits to the code that already calls the existing one? If yes, it must be built behind an interface from the start, even while only one implementation exists, so the second one is a plug-in, not a refactor.

## Consequences

### Positive

- Logic is directly testable and runnable without any UI, and reusable across any number of future frontends.
- Contributors and coding agents have an unambiguous rule for where new code goes, from the very first commit.
- Presentation layers can be rewritten or replaced without touching business logic.
- The codebase grows by addition — new interfaces, new interchangeable implementations — rather than by widening modification of existing code.
- Every implementation behind a Strategy interface, and every interface behind an Adapter, is independently testable and independently fake-able.

### Negative

- Requires discipline to keep presentation layers thin from the outset, before habits or shortcuts form.
- Introduces an extra indirection (a defined `logic/` entry point, and an interface at each Strategy seam) that must be designed even while only one implementation exists yet.

### Mitigations

- Code review (human or agent) should treat an import of `src/tui/` or `src/gui/` from within `src/logic/` as a bug.
- Keep `logic/`'s public entry points explicit and documented, so wrappers have no reason to reach past them.
- Identify Strategy seams up front, before the first implementation behind them is written, rather than waiting for a second implementation to force a retrofit.

## Alternatives considered

### Keep logic and orchestration split across separate top-level folders

Rejected as a top-level split. Orchestration is still computation, not presentation, so it stays inside `src/logic/` rather than as a sibling of it. This keeps the top-level boundary simple: there are only two kinds of code in `src/` — logic, and the wrappers around it.

### Let each interface own a private copy of relevant logic

Rejected. It leads to duplicated or diverging behavior between interfaces as they evolve independently. A single `logic/` surface shared by all wrappers keeps behavior consistent.

### Let behavior variation be expressed as conditional branches inside shared code instead of behind a Strategy interface

Rejected as the default. Conditional branching on a growing set of cases inside otherwise-shared code means every new case requires editing code every other case also runs through, and one case's change can regress another's. A Strategy interface confines a new case to its own new implementation instead.

### Defer the Adapter/Strategy structure until a second interface or implementation actually exists

Rejected. Retrofitting these seams after concrete code already exists means extracting a working implementation into an interface under time pressure, and reworking callers already written against its internals. Deciding the seam before the first implementation is written costs nothing extra and avoids that rework entirely.
