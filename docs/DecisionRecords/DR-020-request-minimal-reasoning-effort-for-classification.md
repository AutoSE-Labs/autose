# DR-020: Request Minimal Reasoning Effort for the Classification Call

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/classifier.py`, the `ChatModel` interface (`src/logic/inference/`)

## Context

DR-003 requires classification to be "one low-cost LLM call." Against a reasoning-capable local model (DR-015's primary target), it was not: classifying the single word "hi" against a locally served Gemma reasoning variant took 74–99 seconds and, more than once, returned nothing — `finish_reason: length` with empty `content`, all 200 output tokens spent on chain of thought that never reached the `{"tier": ...}` answer (DR-016). The classifier's reasoning-channel scan (added to recover a tier mention from a cut-off chain of thought) turned that failure from a guaranteed default into a plausible guess, but "guess from a truncated thought that happened to say a tier's name in passing" is strictly worse than the model just answering — confirmed in practice, where it misread an off-hand mention while reasoning about the classification task itself ("...if a specific tier is suggested...") as the actual answer, misclassifying "hi" as `forge`.

The underlying problem is not the classifier's parsing — it is that a reasoning model was allowed to reason at all for a call that gains nothing from it. Every millisecond and every one of the 200 output tokens a reasoning model spends thinking about how to classify "hi" is pure waste for this specific call, and directly causes both the latency DR-003 wants to avoid and the misclassification the reasoning-scan fallback then has to guess around.

Testing against the actual target runtime (Ollama, DR-015) found that its OpenAI-compatible endpoint honours the standard OpenAI `reasoning_effort` field: sending `"reasoning_effort": "none"` suppressed the chain of thought entirely, returning the answer directly (confirmed: `{"tier": "spark"}` in ~10 seconds instead of ~10–100 seconds of reasoning). `reasoning_effort` is a real field in the OpenAI chat-completions API surface (used to size effort on o-series reasoning models), not a vendor-specific extension invented for this — so sending it is still "the OpenAI-compatible chat-completions shape," consistent with DR-006, the same way `stream_options` is already sent opportunistically elsewhere in the codebase.

## Decision

`ChatModel.complete()` and `ChatModel.stream()` gain an optional `reasoning_effort: str | None` parameter, sent as the standard OpenAI `reasoning_effort` field when set and omitted entirely otherwise. Per DR-006, no behavior above the `ChatModel` boundary depends on whether a given provider honours it.

`TaskClassifier` sets `reasoning_effort="none"` on its one call. No other caller does: an agent stage's reasoning is legitimate work its answer depends on (DR-016 exists to surface it, not suppress it), and only the classification call is specifically defined as one cheap, low-stakes call that gains nothing from thinking.

The reasoning-channel scan added to `TaskClassifier._read()` stays as a second line of defence for providers that do not honour `reasoning_effort` (DR-006 forbids assuming any provider capability), not as the primary mechanism.

## Consequences

### Positive

- Classification against a reasoning-capable local model dropped from 74–100+ seconds (with a real chance of an empty, wasted response) to consistent single-digit seconds with a direct answer, confirmed against the actual target runtime.
- Removes the specific misclassification mode where a truncated chain of thought is scanned for a tier name and happens to mention the wrong one.
- No new dependency and no vendor SDK: the field is part of the OpenAI chat-completions surface DR-006 already targets, sent optionally exactly like `stream_options`.

### Negative

- A provider whose reasoning model does not honour `reasoning_effort` still pays the full thinking cost for classification; the reasoning-channel scan is what catches that case, not this decision.
- One more optional field for every `ChatModel` implementation to pass through correctly, though passing it through unconditionally when `None` (i.e., omitting it) keeps a compliant implementation a one-line addition.

## Alternatives considered

### Increase the classifier's output token budget so reasoning has room to finish

Rejected. Reasoning length for a "thinking" model is unbounded and unpredictable; there is no budget that is reliably enough headroom without also reliably making a trivial classification call slow, which is the exact problem DR-003 exists to avoid.

### Use Ollama's native `/api/chat` endpoint, which already supports `think: false` reliably

Rejected. DR-015 explicitly restricts a native endpoint to enumeration and property discovery — "it is never used to run one [inference]" — and running classification through it would both violate that and introduce a second, provider-specific wire protocol into a codebase DR-006 requires to speak one shape only.

### Rely solely on the reasoning-channel scan and accept the latency

Rejected as the primary fix. It turns a solvable waste of time into a merely-recoverable one, and the scan can still misfire on a chain of thought that mentions multiple tiers before being cut off — exactly what was observed. Suppressing reasoning at the source fixes the actual cause; the scan remains only as a fallback for providers that do not cooperate.
