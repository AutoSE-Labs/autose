# DR-019: Nudge a Silent Turn Rather Than Restarting the Stage

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/agent.py`, the shared agent loop. **Refines DR-016 and DR-009.**

## Context

Observed against a live Ollama 0.31.1 serving a 12B reasoning model, running the `craft` pipeline on a real task: the Plan stage called `search_files`, called `read_file`, received both results — and then returned a turn with empty content, no tool calls, and `finish_reason: "stop"`. It had gathered exactly what it needed and simply stopped without writing anything.

DR-016 already ensures this is *detected* rather than reported as a blank success. What it does not say is what to do next, and the obvious answer turns out to be the wrong one. The runner treated the empty turn as a stage failure and re-ran the stage from a clean `AgentMemory`, per DR-004's rule that a retried stage starts fresh. The fresh attempt issued the same two tool calls, got the same two results, and stopped again. Three attempts, three identical dead ends, and the stage failed after 148 seconds having done the same work three times.

The retry feedback made it worse rather than better: "Your previous attempt stopped before finishing. Produce the complete output this time" is not actionable, because from the model's point of view the new attempt has no previous attempt — it has an empty context and the original request.

Two distinct problems are tangled here. A stage that produced a *wrong* output should be retried from scratch, because its reasoning led somewhere bad and DR-004 is right that a retry should not inherit it. A stage that produced *no* output has not reasoned badly at all; it has gathered good evidence and failed to write a conclusion. Discarding the evidence is precisely the wrong response.

## Decision

When a turn returns no content and no tool call, and nothing has been drafted earlier in the call, the shared agent loop appends a short user turn asking for the final answer — **within the same call**, so every tool result already gathered is still in context — and continues the loop.

There are two nudges, chosen by whether the call has executed any tool yet, because a silence before gathering and a silence after gathering need opposite advice:

- **After at least one tool call** — the model has what it needs; it is told not to call further tools and to write the complete response now.
- **Before any tool call** — nothing has been gathered, so it is told to start: read what it needs from the workspace, then write the response.

Using the first wording for both was tried and is actively harmful. Observed on a live `craft` run: the Validate stage returned nothing on its very first call, was told "you have already gathered what you need — do not call any more tools", and stayed silent, because it had gathered nothing and had just been instructed not to. A nudge that forbids the only action that could make progress is worse than no nudge at all.

This is bounded at two nudges per agent call by default. After that the call ends with its existing `empty_answer` outcome and the stage-level retry takes over as before, so the bound-on-bound structure DR-009 requires is unchanged: a nudge cannot extend a call indefinitely, and a stage retry cannot extend a workflow indefinitely.

The nudge is emitted as an `agent_nudged` event, so a silent model is visible in the event log rather than looking like an unexplained pause.

### `finish_reason` distinguishes two silences

A turn stopped by the output cap (`finish_reason: "length"`) gets the distinct outcome `output_budget_exhausted` rather than `empty_answer`. They look identical to the loop but call for opposite advice: one means the model chose to say nothing and should be asked again, the other means it had no room left and asking again will fail the same way — the user needs a narrower request or more reserved output.

### This does not weaken DR-004's clean-retry rule

DR-004 requires a *stage* re-invocation to start with a clean `AgentMemory`, so a retry does not inherit reasoning that led to a bad artifact. That is untouched. A nudge is not a re-invocation: it is a continuation of one call that has produced nothing to inherit. There is no stale reasoning to protect against, only evidence to preserve.

## Consequences

### Positive

- A stage that gathers correctly and stops now completes for the cost of one short round, instead of failing after re-doing the same tool calls three times.
- Tool results are preserved exactly when they are most expensive to reproduce — on hardware where reading two files took the better part of a minute.
- "Ran out of room" and "chose to say nothing" are separable, so the user gets advice that matches what happened.
- A silent model appears in the event log rather than as an unexplained gap.

### Negative

- A model that is silent because the request genuinely confuses it now costs two extra rounds before the stage fails, making the eventual failure slower.
- The nudge is chosen from one bit of state — whether a tool has run — which is a coarse proxy for "does this call have what it needs". A stage that called one irrelevant tool and then went silent gets told to stop gathering when it should keep going.
- The nudge is another instruction in the context, and a model that follows it too literally may produce a hurried answer rather than the considered one it was building toward.
- It treats a symptom of weak instruction-following rather than fixing the prompt that produced it. A better stage prompt might avoid the silence; this makes the silence survivable either way.

### Mitigations

- Two nudges is deliberately small. If a model needs more than that, the stage-level retry and ultimately DR-009's caps still stop the run.

## Alternatives considered

### Treat the empty turn as a stage failure and retry from scratch

Rejected — this is the behaviour being replaced. It discards the evidence the model just gathered, and the fresh attempt has no memory of having failed, so it reproduces the same dead end. Observed doing so three times in a row.

### Carry the failed attempt's context into the stage retry

Rejected. It fixes this case by breaking DR-004's rule that a retried stage starts clean, which exists to stop a genuinely bad line of reasoning from contaminating the next attempt. Nudging inside the call gets the same benefit without touching that rule.

### Raise the reserved output budget so the model has more room

Rejected as the fix for this. The observed turn had `finish_reason: "stop"`, not `"length"` — the model was not out of room, it decided it was done. Raising the budget would address the other failure, which is now reported separately and does deserve that response.

### Retry the identical request once with a higher temperature

Rejected. It spends a full generation gambling on sampling noise, where a two-sentence nudge addresses the actual problem — the model does not know it still owes an answer.
