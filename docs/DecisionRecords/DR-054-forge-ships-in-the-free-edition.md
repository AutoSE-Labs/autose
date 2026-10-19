# DR-054: `forge` Ships in the Free Edition, Without the Decision Record

- **Status:** Accepted
- **Date:** 2026-10-19
- **Decision owners:** AutoSE maintainers
- **Scope:** Product edition strategy. **Partly supersedes DR-032**: the `forge` tier moves out of the Pro-only list. The Design stage's decision record (DR-018) and the knowledge layer (DR-028) stay Pro-only.

## Context

DR-032 made the `forge` tier Pro-only together with its Design stage that
writes an architecture decision record. Only the second half of that is what an
organisation pays for. The tier itself, a design pass in front of Plan, is
useful to anyone making an architecture-level change, and in the free edition
those requests were being classified `craft` and starting to plan with no
design step at all.

## Decision

- The free edition has the `forge` tier: Design → Plan → Execute → Validate,
  with a validator able to loop back to Design.
- The free Design stage is **read-only**. It reads the repository, may run a
  command to inspect it, and produces a design note: the problem, the chosen
  approach, its consequences, and the alternatives rejected with reasons. The
  note is the `design_doc` artifact, handed to Plan, Execute and Validate as the
  approach already decided. It writes no file, so there is no decision-records
  directory, no write guard, and no ADR in the repository.
- Still Pro-only: the Design stage writing a decision record confined to the
  records directory (DR-018), and the knowledge layer (DR-028): the standards
  pack, retrieval and citation checking of decision records, `remember_fact`,
  `/knowledge` and "Learned facts".

`tuning.forge` and the `prompts.design` override are honoured in the free
edition. A Pro config that sets them works unchanged.

## Consequences

- Classification can answer `forge`, so architecture-level requests get a design
  pass in both editions. Free and Pro now differ in what Design *produces* (a
  note versus a record), not in which tiers exist.
- A free-edition design note lives in the run's artifacts and the session file,
  not in the repository. Anyone who wants it kept as a record is describing
  what Pro sells.
- DR-032's "built by deletion" rule is relaxed for the Design stage: the free
  tree's stage has no guard and no write tools where Pro's has both. Carrying a
  Pro change to `stages.py` has to keep that difference.
