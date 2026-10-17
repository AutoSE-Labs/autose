# Contributing to AutoSE

## Read the Decision Records first

[`docs/DecisionRecords/`](docs/DecisionRecords/) is the spec, not background reading. Every DR marked `Accepted` is binding: a change either conforms to it, or explicitly supersedes it in a new DR that references the one it replaces. Silently contradicting an accepted DR is the one thing this project treats as always wrong (DR-001).

This matters more here than in most repos because much of the work is done by coding agents, which have no memory between sessions beyond what is committed. The DRs are how intent survives.

## Write a DR for anything notable

The bar is deliberately low: **if a reasonable contributor could ask "why was it done this way?" and the answer isn't obvious from the code, it gets a DR.** New capabilities, module boundaries, non-trivial implementation choices where alternatives existed, reversals of earlier decisions, tooling changes others need to know about.

A bug fix, a typo, or a one-line config tweak does not.

Write it alongside the change, not afterwards. Number sequentially, name it `DR-NNN-short-slug.md`, and use the structure the existing records use: metadata, **Context**, **Decision**, **Consequences** (positive, negative, mitigations), **Alternatives considered**. The alternatives section is not filler — a rejected option with its reason is what stops the same debate being re-run in six months.

Add a row to the README's DR table in the same change.

## Where code goes

```text
src/logic/   all computation — must be fully usable with no UI attached
src/tui/     terminal adapter — rendering, input, wiring. No business logic.
src/gui/     web dashboard adapter. Same rules as tui/, and it must stay
             deletable — nothing outside it may import it (DR-029).
```

Dependencies point one way: adapters depend on `logic/`; `logic/` never imports an adapter (DR-002). If you are unsure whether something is logic or presentation, that rule is the test.

Adapters call `logic/`'s public entry points — chiefly `logic.session.Session` — and never construct an agent, a tool registry, or a chat model themselves. A second interface should be one new adapter, with no edit to `logic/`.

### Behaviour that could vary goes behind a Strategy interface

Not an `if/elif` that grows a branch per case. The test: *could a second, materially different implementation be added by writing one new module and registering it, with zero edits to the code that already calls the existing one?* If yes, build the interface now, even while only one implementation exists (DR-002).

Existing seams to follow as examples: `ChatModel` and `ModelCatalog` (inference), `Workflow` (per-tier shape), `ApprovalGate`, `EventSink`, `TokenEstimator`, `ToolOutputStore`.

## Boundaries that are enforced, not just documented

`tests/test_architecture.py` fails the build if:

- anything in `logic/` imports `tui` or `gui`;
- a top-level package appears in `src/` other than `logic`, `tui`, `gui`;
- `src/core/`, `src/runners/`, or `src/orchestration/` reappears — orchestration lives *inside* `logic/` (DR-002);
- a retired tier name (`lite`, `standard`, `advanced`) shows up as an identifier or string literal — the tiers are `spark` and `craft` here, plus `forge` in Pro (DR-003, DR-032);
- anything in `logic/` or `tui/` imports `gui`, which would stop the dashboard from staying a removable adapter (DR-029).

This is the free edition. It is derived from the Pro source by removing the `forge` tier and the knowledge layer (DR-032). Do not reintroduce them here.

If one of these fails, the fix is the code, not the test.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

Tests are named for the behaviour they pin, not the function they call, and each should fail for exactly one reason. Where a DR specifies behaviour, the test says so — a reader should be able to find the rule a test protects.

Prefer the fakes in `tests/support/fakes.py` over mocking internals: `ScriptedChatModel` replays completions and records what was actually sent, `RecordingApprovalGate` scripts approval decisions. Both are ordinary implementations of the Strategy interfaces above, which is the point.

## Safety properties that are not negotiable

These are enforced in the shared layer every adapter passes through, so a new interface inherits them. Do not re-implement or route around them:

- **Approval gating** (DR-012). `run_command`, `web_search`, and `fetch_url` are surfaced to the user with their real arguments and block on a decision. The default with no gate attached is *deny*. Auto-approval is opt-in per run and must name who authorised it.
- **Least-privilege tool registries** (DR-008). A stage receives its own cloned registry containing only the tools it is entitled to. A validation-equivalent stage has no write or edit tool *in its schema at all* — not merely a prompt telling it not to write.
- **Workspace confinement.** Every path resolves through `Workspace`, which refuses anything outside the root, symlink targets included.
- **Fail-closed bounds** (DR-005, DR-009, DR-010, DR-030). A budget exhausted, a cap hit, a run stopped by the user, or a request that will not fit is a distinct typed error. None of them may be reported as a success. Bounds are unlimited by default and read `-1` through `logic.limits`; if you add a cap, give it that treatment and check the cancel token wherever the new loop could run long.
- **The requirements ledger is the completion signal** (DR-005). Every stage echoes back exactly the IDs it was given, the runner re-parses the verifying stage's ledger itself, and a claimed pass with anything unverified becomes a loopback. Do not add a path where a stage's own verdict is taken at face value.

## Working on the multi-stage tiers

`craft` runs on `workflows/staged.py`, which takes its stage list as a parameter. Pro's `forge` is the same runner with a Design stage in front, so keep runner changes stage-list-agnostic: that is what lets a change carry between the editions.

A stage is a `StageSpec` in `workflows/stages.py`: a prompt, a tool-name list, and its ledger vocabulary. Adding a stage should not require touching the runner.

Prompts live in `workflows/prompts.py` and are written for small local models (DR-015), which changes what good means: state the output shape before the task, say what the stage may not do as well as what it should, and keep them short — every token is spent on every round of every stage, against a context that may be 4k.

**Test against a real local model before believing a stage works.** Three of the decisions recorded here (DR-016, DR-017, DR-019) came from behaviour no fake reproduced: a reasoning model returning empty content, a model appending prose after the ledger, and a model gathering evidence and then falling silent. The fakes in `tests/support/` are for pinning behaviour once you understand it, not for discovering it.

## Commit and PR

Explain *why*, not what — the diff already shows what. Reference the DR the change implements or supersedes. If a change needed a new DR, it belongs in the same commit series.
