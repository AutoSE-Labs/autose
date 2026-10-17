# DR-017: The Ledger Tolerates Presentation Variance, and the Verdict Is Derived, Not Asked For

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/requirements.py`, `src/logic/workflows/verdict.py`. **Implements DR-005 — does not supersede it.**

## Context

DR-005 fixes the ledger contract: IDed requirements, echoed back exactly, parsed deterministically, with a claimed pass independently re-checked. It deliberately does not say how strict the *parser* should be, or how the runner should learn what the verifying stage concluded. Both questions turn out to have answers that matter on the models DR-015 targets, and getting either wrong is expensive in a way that is easy to miss when testing against a frontier model.

**On strictness.** DR-005 says a formatting slip fails the stage. Taken to its limit, that means an em dash instead of a hyphen fails a stage. A lowercase `implemented` fails a stage. A closing sentence after the ledger fails a stage. Every one of those costs a complete stage re-run — minutes of wall clock on the hardware this product is for, measured here at roughly two tokens per second — to gain nothing, because none of them changes what the stage actually claimed. A gate that fires on typography rather than on substance does not add rigour; it adds cost, and pressure to disable the gate.

**On the verdict.** The natural implementation, and the one the prior codebase used, is to ask the model for its verdict as structured JSON in a second call after the report. That has three problems. It is a whole extra generation, spent restating a conclusion already present in the report. It leans on structured-output reliability that DR-006 explicitly forbids assuming, since not every OpenAI-compatible endpoint offers it. And it is redundant on its face: DR-005 already requires the runner to re-derive the outcome from the ledger and override a claimed pass, so the asked-for verdict is never the deciding input anyway.

## Decision

### Presentation variance is tolerated; semantic variance is not

The parser accepts, as equivalent:

- an em dash, en dash, or hyphen as the separator;
- `-` or `*` as the bullet marker;
- any capitalisation of a status name, normalised to its canonical spelling;
- a heading at any level, and any capitalisation of `REQUIREMENTS STATUS`;
- prose after the ledger — the section ends at the first line that is not a ledger item, once at least one item has been read.

It continues to reject, as before:

- a missing or empty ledger;
- a duplicate requirement ID;
- a status belonging to a different stage's vocabulary — `Implemented` must never be readable as `Verified`;
- a malformed line before any valid item, which means the format is wrong rather than merely finished.

The dividing line is whether the variation changes *what was claimed*. It never does for any item in the first list; it always does for the second.

### Trailing prose is safe because the ID contract is the real gate

Ending the section at the first non-item line means a malformed line *after* valid items is skipped rather than rejected. This is safe, and it is worth being explicit about why: `require_same_ids` compares the parsed IDs against the canonical ledger, so a requirement lost to a malformed line downstream surfaces as "dropped R2" — the stage still fails, with a clearer message than a regex complaint. The tolerance relaxes the parser, not the contract.

The one place without that backstop is the planning stage, which defines the IDs and so has nothing to compare against. There, a malformed trailing line yields a plan with fewer requirements rather than a failed stage. That is an accepted, bounded loss: it makes the run less rigorous, not wrong, and the alternative fails the common case to protect the rare one.

### The verdict is derived from the report and the ledger, with no second call

The runner reads a single `VERDICT: PASS` / `VERDICT: FAIL <stage> — <reason>` line and combines it with the independently parsed ledger:

- **Any requirement not `Verified`** → loopback, naming the specific unmet IDs, regardless of what the line says. A claimed pass here is recorded as *overridden*, so the gate is visibly seen to have done something.
- **An explicit `FAIL`** → loopback, honoured even with every requirement verified. The ledger is necessary but not sufficient: broken existing behaviour or a contradicted decision are legitimate grounds to reject a round that satisfies every requirement.
- **No readable verdict line** → loopback. An unreadable gate is not a passed gate.
- **`PASS` with every requirement verified** → pass.

A loopback target naming a stage the running tier does not have — `design` under `craft` — is routed to the earliest stage that exists rather than failing the runner.

## Consequences

### Positive

- A stage is failed for what it claimed, not for how it punctuated. On slow local hardware this is the difference between a workflow that converges and one that burns its iteration budget on formatting.
- One fewer generation per validation round, and no dependence on structured-output support the endpoint may not have.
- The pass/fail decision has one implementation and one place to read it, rather than a model-asserted verdict and a runner-derived one that could disagree.
- Every rejection path stays fail-closed: unverified, explicitly failed, and unreadable all loop back.

### Negative

- A malformed ledger line after valid items is skipped rather than reported, and in the planning stage specifically nothing downstream catches it. A plan can silently carry fewer requirements than intended.
- Reading a verdict line with a regex is its own small parser, with its own failure mode — one that fails safe, but still costs a loopback when it fires spuriously.
- The tolerance list is a judgement about which variations are meaningless. A future status name that differs from another only by case would break that assumption, and the parser would not notice.

### Mitigations

- `require_same_ids` covers the skipped-line case everywhere except the plan, and the plan's ledger is shown to the user, who can see it is short.
- Status vocabularies are disjoint per stage and checked, so case normalisation cannot promote a status across stages.

## Alternatives considered

### Reject every deviation, exactly as DR-005 reads at its strictest

Rejected. It fails stages over typography, and each failure costs a full re-run. The predictable result is that the gate gets relaxed or switched off under time pressure, which loses far more rigour than tolerating a hyphen ever would.

### Ask the model to repair its own malformed ledger in a follow-up call

Rejected as the primary mechanism. It is an extra generation to fix something a parser can simply accept, and it invites the model to rewrite content while "fixing formatting". The runner does already re-attempt a stage with the parser's own complaint as feedback, which covers genuinely wrong output — this decision is about not invoking that path for variations that were never wrong.

### Ask the model for a structured JSON verdict in a second call

Rejected. A whole extra generation to restate a conclusion the report already contains, depending on structured-output reliability DR-006 forbids assuming, to produce an input the runner must override anyway.

### Infer the verdict from the ledger alone, with no verdict line

Rejected. It would make a fully-verified ledger an automatic pass, removing the validator's ability to reject a round on grounds outside the ledger — broken existing behaviour, a contradicted decision record — which DR-005 explicitly preserves.
