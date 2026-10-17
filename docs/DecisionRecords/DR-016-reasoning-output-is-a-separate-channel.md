# DR-016: A Reasoning Model's Chain of Thought Is a Separate, Non-Replayed Channel

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/inference/` response normalisation, the shared agent loop, and what Adapters receive

## Context

A large share of the models AutoSE now targets first (DR-015) are reasoning models: they emit an internal chain of thought before, and separately from, their actual answer. The OpenAI chat-completions schema has no field for this, so runtimes invented their own — Ollama returns `reasoning` on the message and as a `delta.reasoning` in a stream, others use `reasoning_content` or `thinking`. Verified against a live Ollama 0.31.1 serving a reasoning model: `content` stays `""` for the entire thinking phase while `reasoning` streams token by token, and on a tool-calling turn the message carries reasoning, empty content, and a populated `tool_calls` array together.

Ignoring the field is not a neutral choice, it is three separate bugs:

1. **A blank answer.** The loop reads `content`, finds `""`, sees no tool calls, and reports a successful turn with nothing in it. The user sees the model "return nothing" when it in fact spent its entire output budget thinking.
2. **No visible progress.** On the local hardware DR-015 targets — measured here at roughly two tokens per second — a model can think for a minute or more before a single character of answer exists. With reasoning discarded, the interface has nothing to show, and an interface with nothing to show is indistinguishable from one that has hung.
3. **Unbounded context growth, if naively included.** The obvious fix — treat reasoning as content — is worse than the bug. It concatenates the thought process into the answer the user reads, and replays every previous round's full reasoning back to the model on the next round, growing the request by the model's entire deliberation each time.

Separately, models leak raw chat-template control tokens (`<|channel>`, `<|end|>`) into this field, and because deltas are arbitrarily split, a marker routinely arrives across two or more chunks.

## Decision

Chain of thought is a **first-class but separate channel**, normalised at the provider boundary and never conflated with the answer.

### Normalised at the boundary

`CompletionResult` gains a `reasoning` field and `StreamChunk` gains a `reasoning` delta alongside `content`. The provider implementation reads whichever of the known non-standard fields the runtime used and populates them. Above that boundary nothing knows which field name any runtime chose, exactly as DR-006 requires.

### Never replayed to the model

`CompletionResult.to_message()` — the assistant message appended to the running history — deliberately omits reasoning. It is non-standard, providers do not expect it echoed, and replaying it would grow every subsequent request by the whole prior deliberation.

### Delivered to Adapters as events, not as answer text

The agent loop consumes reasoning deltas and re-emits them as `agent_reasoning_delta` events. An Adapter iterating an agent or session stream therefore receives the answer and only the answer, while still being able to render thinking distinctly — dimmed, collapsed, or not at all. Which of those it does is a presentation decision, which is why it belongs to the Adapter and not here (DR-002).

### An empty turn is reported, not disguised

A turn with no answer text and no tool call gets the distinct stop reason `empty_answer` rather than being reported as a successful blank response, consistent with DR-009's rule that a bounded loop must never present exhaustion as success.

### Control tokens are filtered with a hold-back buffer

Template markers are stripped from the reasoning channel by a filter that holds text back from an opening `<|` until its closing `>` arrives, so a marker split across deltas is still caught. A held-back fragment that never closes is released after a bounded length, so literal `<` in prose is not swallowed. Only the reasoning channel is filtered: the answer channel is passed through untouched, because a legitimate answer may contain those characters.

## Consequences

### Positive

- A reasoning model produces a correct, non-empty answer instead of an apparently blank one.
- The interface has something truthful to show during a long thinking phase, which on slow local hardware is the difference between "working" and "hung".
- Context does not grow by the model's deliberation each round.
- The distinction between "thought about it and answered" and "thought until the budget ran out" is visible in the stop reason rather than silently identical.
- An Adapter chooses whether thinking is shown; `logic/` does not decide presentation.

### Negative

- Another non-standard field set to track. A runtime using a fourth name will have its reasoning dropped until that name is added — a silent degradation, not an error.
- Reasoning is discarded after being emitted, so it is not available for later inspection or debugging within a run.
- The hold-back filter briefly delays reasoning output and, in the pathological case of a long literal `<|` in prose, emits it late rather than never.
- Reasoning tokens are still billed and still consume the output budget, so a verbose reasoning model gets less room for its actual answer — mitigated by DR-015's scaled output reservation, not solved by it.

## Alternatives considered

### Ignore the field and read only `content`

Rejected. It is the status quo bug: a reasoning model appears to return nothing, and the interface has nothing to display through the longest part of the wait.

### Treat reasoning as content

Rejected, and worse than ignoring it. The thought process ends up in the answer the user reads, and replaying it each round grows the request by the entire prior deliberation — a compounding cost on exactly the small-context local models DR-015 targets.

### Yield reasoning into the same stream Adapters iterate, tagged

Rejected. Every Adapter would then have to filter the stream correctly to avoid rendering thinking as the answer, and one that forgot would be subtly wrong rather than obviously so. Events already exist for out-of-band information, and the type system keeps the channels apart.

### Strip control tokens with a regex over each delta

Rejected. Deltas split markers across chunks routinely — a `<|channel>` arriving as `<|`, `channel`, `>` matches no per-delta pattern — so a stateless filter catches the easy cases and misses the common one.

### Ask each runtime whether the model is a reasoning model and branch

Rejected. Capability reporting for this is inconsistent where it exists at all, and the branch buys nothing: reading whichever reasoning field is present costs the same whether or not one was expected.
