# DR-032: The Pro Edition Is the Decision-Record and Knowledge Layer; the Dashboard Ships in Both

- **Status:** Accepted; the `forge` tier itself is partly superseded by [DR-054](DR-054-forge-ships-in-the-free-edition.md), which brings it to the free edition without the decision record
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers, on direction from the project founder
- **Scope:** Product edition strategy. **Supersedes DR-013.** Changes which of DR-003's tiers and DR-004's stores each edition ships; DR-018 and DR-028 become Pro-only.

## Context

DR-013 made the web dashboard (`src/gui/`) the only difference between the
editions and kept everything else, `forge` and the knowledge stores included,
in both. It said that a later decision might restrict capability instead, and
that such a restriction had to supersede DR-013 rather than creep in through an
unrelated change. This is that decision.

The direction changed for two reasons. First, the dashboard is now the easiest
way into AutoSE for someone who does not live in a terminal. Holding it back
from the free edition would keep that person away, which is the opposite of
what a free edition is for. Second, the features that matter most to an
organisation are the ones about recorded decisions: a Design stage that writes
an architecture decision record, retrieval that grounds a run in the
repository's existing records and an organisation's standards, citation
checking, and the store of approved learned facts. Teams and companies pay for
those. An individual developer fixing a bug does not need them to get value
from the tool.

## Decision

### What is Pro-only

- **The `forge` tier** (DR-003) and its **Design stage** that writes a decision
  record, confined to the records directory (DR-018).
- **The knowledge subsystem** (DR-004's durable stores, built in DR-028):
  - the organisation standards pack (`AUTOSE_ORG_KNOWLEDGE`, `knowledge.org_root`)
  - retrieval of the repository's own decision records into an evidence bundle
  - `[repo:…]`/`[org:…]` citation checking and `FabricatedCitationError`
  - the `remember_fact` tool, the operational store
    (`.autose/knowledge/operational.jsonl`), and its review surfaces: `/knowledge`
    in the terminal, "Learned facts" in the dashboard

### What both editions ship

Everything else, unchanged: `spark` and `craft` with the requirements ledger and
fail-closed validation, local runtime discovery, every workspace, git and web
tool, approvals, `/diff` and `/undo`, durable sessions, the JSON adapter, the
terminal interface, and **the web dashboard**, concurrent sessions included.

### How the free edition behaves where Pro has more

- `forge` is not a tier, so a `forge` name read from elsewhere normalises to
  the default tier: a Pro session file, a stale `--tier`, or a model that
  answers `forge` all land on `craft`, never on an error. The classifier prompt
  does not mention it, and architecture-level requests are classified `craft`.
- A validator that asks to loop back to `design` is routed to Execute, the same
  way `craft` handled it before.
- A Pro config's `tuning.forge` section and `prompts.design` override are
  ignored. The prompt override produces the usual unknown-name warning.
  Neither is an error, so one config file works in both editions.

### How the editions are built

Unlike DR-013's GUI split, this one cannot be a directory drop. Removing the
knowledge layer touches `src/logic/` (the staged runner, the stage registries,
the session facade) and both adapters. So the free edition is a **derived
tree**: the `AutoSE-Labs/autose` repository is the Pro source with these
features removed, and nothing else. Work lands in Pro first and is carried to
the free edition, except work on a Pro-only feature, which is not.

## Consequences

### Positive

- The free edition is a complete product for an individual: the dashboard,
  both everyday tiers, sessions and undo, with no crippled feature left visible.
- What Pro sells is clear and easy to explain to an organisation: decisions
  recorded, standards enforced, citations checked.
- `src/logic/` has no edition flag. Each edition's code is simply what it ships,
  so there is no dead branch and no runtime licence check to get wrong.

### Negative

- The sync burden DR-013 avoided comes back. Every change outside the Pro-only
  features has to be carried to the free tree, and a change to `staged.py`,
  `stages.py` or `session.py` may conflict where the two trees differ.
- Without an evidence bundle, a free-edition run cannot ground itself in the
  repository's decision records. It can still *read* them as ordinary files.
- `craft` now takes the architecture-level work that `forge` would have
  designed first, so those runs start planning without a recorded design.

### Mitigations

- The free tree differs from Pro only by deletion. A carried-over change that
  touches a removed feature fails to import or fails a test rather than
  half-applying.
- The free edition's tests assert the Pro-only surfaces are absent:
  `tests/test_stage_permissions.py`, `tests/test_tiers_and_workflows.py`,
  `tests/test_classifier.py` and `tests/test_tui_changes.py` each check that
  `forge`, the Design stage, `remember_fact` or `/knowledge` is missing.

## Alternatives considered

### Keep DR-013: the dashboard as the only differentiator

Rejected. It holds back the feature most likely to bring a new user in, and
gates nothing an organisation would pay for.

### Gate the Pro features at runtime with an edition flag in one tree

Rejected. A flag checked in `src/logic/` puts the edition into the layer DR-002
keeps interface- and edition-agnostic. Shipping the Pro code inside the free
build also makes the gate a patch away from removal. A derived tree costs a
sync step and avoids both problems.

### Make only `forge` Pro-only and keep the knowledge stores free

Rejected. Without `forge`, the stores' main consumer is gone: the evidence
bundle and citation rules exist to ground a design. What remains, the org
standards pack and learned-fact approval, is organisational governance, the
same audience as `forge`.
