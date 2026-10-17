# DR-021: Tuning Knobs and Prompts Live in One Repo-Local `.autose/config.yaml`

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Every numeric/behavioural constant and built-in prompt across `src/logic`; config loading precedence

## Context

By the time `craft`/`forge` landed alongside the classifier and context budgeting, AutoSE had accumulated a real number of hardcoded module-level constants — the context safety factor and reserved-output fraction (DR-010), tool-output bounds (DR-011), the shell command timeout, the batch-edit cap (DR-007), the classifier's output-token budget and reasoning-effort setting (DR-020), session history bounds (DR-004), and per-tier workflow bounds (DR-009) — plus several built-in system prompts (the classifier's, `spark`'s, and each `craft`/`forge` stage's). Every one of these was reachable only by editing source.

That is a real limitation for a product whose whole premise is running against whatever local model a user actually has (DR-015): a small or unusually slow model may need a larger shell timeout, a smaller classification budget, a shorter time budget before giving up, or a system prompt phrased differently for how that specific model responds — and none of that should require forking the codebase to change.

## Decision

Every one of those constants becomes a field on a `Tuning` dataclass (`src/logic/tuning.py`), grouped by the subsystem it tunes (context, tool output, shell, editing, classifier, session, and one `TierBounds` section each for `spark`/`craft`/`forge`, since `craft` and `forge` intentionally keep different defaults). Every prompt gains a name and can be replaced via a `prompts:` mapping, resolved through `logic.prompt_overrides.resolve_prompt()`. Every field's default is exactly the value that used to be hardcoded, so an absent or partial config changes nothing.

Both live in **one file**: the repo's own `.autose/config.yaml`, alongside the runtime settings (`inference`/`workspace`/`approval`) `AutoSEConfig` already read from a config file. This is the single, singular place a user edits to change AutoSE's behavior for a given repo — not a second file, and not scattered across source.

**Config layering changes to make this actually apply.** Previously, config discovery picked the first existing file across an environment override, an OS-specific machine path, and `.autose/config.yaml` in the current directory — whichever was found first was the only one read. That meant a user with a personal machine-level config (set once, for convenience across many repos) would never have a repo's own `.autose/config.yaml` consulted at all, which defeats the purpose of a *repo-local* tuning/prompts file. Loading now always additionally layers the current directory's `.autose/config.yaml` on top of whatever the existing search found (if it is a different file), with the repo-local file's values winning on any key both set. A repo's own file therefore always applies to work done in it, whether or not the user also has a separate machine-level config.

Threading follows the existing seams rather than adding new ones: `WorkflowRequest` (already the per-turn object `Session` builds) gains `tuning: Tuning` and `prompt_overrides: Mapping[str, str]` fields; `SparkWorkflow`/`StagedWorkflow` read them when constructing their `ContextBudget`, `BoundedToolOutput`, tool overrides (`run_command`'s timeout, `edit_file`'s cap, bound via `functools.partial` rather than module-level constants), and system prompt. `TaskClassifier` and `SessionMemory` take the same `Tuning`/prompt-overrides objects directly.

Network-tool bounds (`web_search`/`fetch_url`'s timeouts, byte caps, and SSRF-relevant address checks) are deliberately **not** exposed here: those are security constraints, not preferences, and a config field that could weaken them is a footgun this decision does not introduce.

## Consequences

### Positive

- One documented, editable file per repo covers every tunable knob and every built-in prompt; `write_example_config`'s template lists all of them, commented out at their real default.
- A repo can be tuned for the specific model it targets — a slower shell timeout, a smaller classification budget, a rephrased prompt — without patching source, and the file can be committed and shared with a team.
- `craft` and `forge` keep their intentionally different bounds (30 vs 40 tool rounds, 3600s vs 7200s, etc.) as separate config sections rather than collapsing into one shared value that would erase the distinction.
- Every default is unchanged from what was previously hardcoded, verified end-to-end: an unconfigured repo behaves identically to before this decision.

### Negative

- Config loading now silently reads a second file (the repo-local one) in addition to whichever machine-level file precedence already picked, which a user inspecting only `AUTOSE_CONFIG`/the OS path might not expect. `AutoSEConfig.source` still names only the primary file, not the overlay — a real, accepted diagnostic gap rather than a correctness one.
- `_merged()`'s coercion is a blunt `type(current)(value)` cast; a badly-typed override fails with a `ConfigurationError` naming the section and key, not a precise schema error.
- Every prompt-producing call site now takes an extra optional parameter (`system_prompt_template`, or an overrides mapping); a new stage or tier that forgets to thread it silently keeps its hardcoded default rather than failing loudly — acceptable since "no override took effect" degrades to correct existing behavior, not incorrect behavior.

## Alternatives considered

### A separate file for tuning/prompts, distinct from the runtime `config.yaml`

Rejected. The user asked for one file; DR-004's "different owner, different lifetime" reasoning for keeping org standards out of runtime config does not apply here — tuning and prompts share the same owner (whoever configures this repo for AutoSE) and the same lifetime (this repo, this checkout) as the existing `inference`/`workspace`/`approval` sections.

### Leave `.autose/config.yaml` as the lowest-precedence, only-if-nothing-else-exists fallback

Rejected. That is the status quo problem: a user with any machine-level config would never have their repo's own tuning/prompts file read at all. Layering it on top, always, is what makes a repo-local file actually repo-local.

### A single flat set of workflow bounds shared by `craft` and `forge`

Rejected. `craft` and `forge` already have deliberately different defaults (DR-009's `WorkflowBounds` vs. `forge.py`'s wider `FORGE_BOUNDS`); collapsing them into one shared tunable would silently erase that distinction the first time a repo configured it.

### Expose the network tools' timeouts/byte caps/SSRF checks as tuning too

Rejected. These bound a security property (DR-012's approval gate assumes what it is approving cannot itself be redirected to a local/private address), not a preference; making them configurable would let a config file weaken a safety boundary it should not be able to touch.
