# DR-011: Preserve Oversized Tool Output as Recoverable Artifacts

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Shared tool-result handling across all tiers

## Context

Bounding every tool result to a fixed character limit is necessary to protect the context window, but simply truncating from the front and discarding the rest is lossy in a way that actively hurts debugging: command, compiler, linter, and test output commonly place the decisive failure summary near the *end* of the output, which a front-truncation policy discards.

## Decision

When a tool result exceeds the configured character limit, the shared agent layer will:

1. Save the complete result to a content-addressed file under a workspace-local, git-ignored tool-output directory (derived from the tool name, arguments, and result, so identical calls deduplicate).
2. Return a bounded preview to the model containing both the beginning and the end of the result, with an explicit notice and the artifact's workspace-relative path so it can be recovered in bounded ranges via the existing file-read tool.
3. Retain a bounded number of the most recent such artifacts, removing the oldest on a best-effort basis.
4. Apply owner-restricted file permissions where the OS supports it.
5. Never fail the underlying tool call if artifact persistence itself fails — fall back to the bounded head/tail preview with a notice that the full output could not be saved.

This applies uniformly across all complexity tiers and does not add or change any model-facing tool schema.

## Consequences

### Positive

- Failure summaries at the end of long output remain visible to the model instead of being silently discarded.
- Complete evidence remains recoverable on disk without spending context tokens on it up front.
- No new tool schema/token cost, and identical repeated tool calls deduplicate via content-addressed naming.

### Negative

- Oversized results consume bounded local disk space.
- Saved output may contain sensitive command output and remains locally readable until retention rotates it out.
- Cleanup by recency is best-effort, not a durable index.

## Alternatives considered

### Keep only the first N characters and discard the rest

Rejected. This is the status quo problem this decision fixes — it can hide the actual failure and makes some command output permanently unrecoverable.

### Return only the tail of command-type tool output

Rejected. Would require tool-specific truncation semantics in a layer meant to be shared across all tools; a head-and-tail preview is a simpler, general-purpose default.

### Add a dedicated tool-output-retrieval function

Rejected. The existing bounded file-read tool already supports ranged retrieval within the workspace; a second retrieval schema would add tokens without adding capability.
