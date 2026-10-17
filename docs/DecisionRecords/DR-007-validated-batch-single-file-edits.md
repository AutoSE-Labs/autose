# DR-007: Validated, Batch Single-File Edit Tool

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** File-editing tool exposed to implementation-capable agents

## Context

A file-edit tool that only performs one exact old/new string replacement per call forces multiple tool round-trips (and repeated file reads) for any file needing several changes, and makes the outcome dependent on the order calls happen to arrive in. Alternatives seen elsewhere include multi-file patch languages (effective mainly for model families specifically trained on that patch syntax) and broad fuzzy-matching editors backed by significant recovery infrastructure (locks, checkpoints, stale-read tracking). Neither fits a product aiming to work well with a wide range of models, including smaller/local ones, while keeping the tool surface small and auditable.

## Decision

Keep one edit tool, and give it a bounded batch schema: a target file path plus an array of one to twenty `{old_str, new_str}` replacement pairs in a single call.

The editor:

1. Resolves the target inside the workspace via existing path-boundary checks.
2. Requires every `old_str` in the batch to match exactly once against the *original* file content (not against intermediate results of earlier replacements in the same batch).
3. Rejects the entire batch — leaving the file byte-for-byte unchanged — on any empty match, no-op replacement, ambiguous match, or overlapping range.
4. Applies all validated replacements in one pass, from the end of the file toward the beginning, so replacement order in the request cannot affect the result.
5. Preserves file encoding characteristics (UTF-8 BOM, consistent CRLF line endings) through a successful edit.
6. Writes the file once per successful call and reports a concise changed-block count.

A full-file write tool remains available separately for new files and intentional complete rewrites, with its own description explicitly steering targeted modifications toward the batch edit tool instead.

## Consequences

### Positive

- Multiple changes to one file collapse into one tool round-trip instead of several.
- Validation is independent of replacement order, and a failed batch never partially mutates the file.
- The tool surface does not grow (one edit tool, not two competing formats), which matters for models not specifically trained on a patch syntax.

### Negative

- Multi-file changes still require one call per file; this decision does not introduce cross-file atomic patches.
- Exact string matching can fail if the file was modified externally between read and edit — no fuzzy-matching fallback is provided by design.
- Mixed line-ending files receive plain exact matching only; only consistently-CRLF files get line-ending preservation.

## Alternatives considered

### Add a separate multi-file patch-language tool

Rejected for now. Patch-syntax reliability is model-dependent, and introducing it without evidence a given model reliably produces valid patches would add failure surface without proven benefit.

### Add fuzzy/approximate matching for replacements

Rejected. Broad fuzzy matching risks applying a plausible-looking edit to the wrong location; exact unique matching is easier to audit and reason about, even though it is less forgiving of stale reads.

### Keep one replacement per tool call

Rejected as the default. It is simple but creates avoidable tool round-trips and context growth for the common case of multiple related changes in one file.
