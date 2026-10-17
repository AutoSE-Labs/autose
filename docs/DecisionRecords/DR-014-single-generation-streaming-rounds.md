# DR-014: Streaming Rounds Carry Their Own Tool Calls (One Generation per Round)

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/inference/` `ChatModel` interface and the shared agent loop in `src/logic/common/agent.py`

## Context

An interactive interface wants the model's final answer rendered token by token, while the agent loop needs each round's *tool calls* in structured form to decide whether there is another round at all. Those two needs pull in opposite directions, because a chat-completions stream delivers tool calls as incremental deltas rather than as a finished object.

The obvious way to sidestep the conflict — and the one the prior implementation used — is to run every round non-streaming, and then, once a round comes back with no tool calls, discard that round's text and re-issue the *same* request with `stream: true` purely to obtain the answer again as deltas. That is simple, but it generates the final answer twice: the whole answer is produced non-streaming, thrown away, and produced again. DR-006 commits AutoSE to running well on self-hosted and local models, where a second full generation of the longest output in the request is among the most expensive things the system could choose to do, and on slow hardware it is plainly visible as a stall before any text appears.

The two also differ in *content*, not just cost: the discarded and re-generated answers come from two separate samplings and need not agree, so what the user reads is not the turn the loop actually observed.

## Decision

`ChatModel` exposes streaming as `stream(...) -> Iterator[StreamChunk]`. Content arrives incrementally in `StreamChunk.content`; the **final** chunk of every stream carries `StreamChunk.result`, a fully assembled `CompletionResult` equivalent to what `complete()` would have returned for the same request — content, tool calls, usage, and finish reason.

Reassembling that result from SSE deltas — in particular stitching tool-call arguments back together across chunks, which are addressed by `index` rather than by `id` — is the responsibility of the provider implementation, at the DR-006 integration boundary. It is not visible above that boundary.

Consequently the shared agent loop has exactly one shape for both modes. A round issues either `complete()` or `stream()`, obtains one `CompletionResult` either way, and proceeds identically. A streaming round never issues a second request to recover its tool calls, and the text the user sees is the text of the turn the loop acted on.

Content deltas from a round that turns out to contain tool calls are forwarded to the Adapter as they arrive, before that is known. In the staged workflows this loop serves, such rounds normally carry no content at all; when they do, it is the model reasoning aloud on the way to a tool call, which is legitimate interim output for an Adapter to render.

## Consequences

### Positive

- One generation per round in every mode: the final answer is produced once, not twice. On a local model this removes what would otherwise be the single largest avoidable latency and token cost in a request.
- The streamed text is the turn the loop observed, so the rendered answer cannot diverge from the one the workflow acted on.
- One loop, not two: streaming and non-streaming rounds cannot drift apart in behaviour, because they differ only in how the round's result was obtained.
- Provider streaming quirks stay behind the `ChatModel` boundary, as DR-006 requires.

### Negative

- Every `ChatModel` implementation must reassemble streamed tool calls correctly, which is more work than forwarding content deltas — a per-provider obligation, and a real source of subtle bugs if a provider's delta format is unusual.
- Interim content from a round that turns out to be a tool-call round has already been emitted by the time that is known, so an Adapter that renders it must be able to supersede it.
- A provider that ends a stream without a usable terminal state yields an empty result for that round; the loop treats it as an answered turn with no content rather than retrying.

### Mitigations

- A provider whose streaming format cannot be reassembled reliably can fall back to `complete()` inside its own `stream()` and emit a single final chunk. The reference `openai-compatible` implementation already does exactly this when a backend rejects `stream: true`, so a non-SSE backend remains fully usable.

## Alternatives considered

### Run rounds non-streaming, then re-request the final answer as a stream

Rejected. This is the prior implementation's approach and the reason this DR exists: it generates the final answer twice, and the text shown to the user comes from a different sampling than the one the loop observed.

### Stream only the content and re-issue a non-streaming call whenever tool calls are suspected

Rejected. "Suspected" is not knowable without parsing the tool-call deltas — which is the work this decision does anyway — so it pays the same implementation cost while keeping the double-generation for exactly the rounds that matter most.

### Do not stream at all; render each round's answer once it is complete

Rejected. Waiting for a complete answer with no visible progress is the worst experience precisely on the slow local hardware DR-006 commits to supporting, and the interface layer has no way to improve on it.

### Buffer content deltas until the round is known not to be a tool-call round

Rejected. It removes the possibility of superseded interim text at the cost of removing streaming's only benefit — text appearing as it is produced — since the round is only known to be final once generation has finished.
