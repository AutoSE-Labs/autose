# DR-022: Budget Against the Window the Runtime Actually Serves

- **Status:** Accepted
- **Date:** 2026-08-30
- **Decision owners:** AutoSE maintainers
- **Scope:** Context budgeting and model enumeration; refines DR-010 and DR-015

## Context

DR-010 budgets each request against a context limit, and DR-015 makes discovery
responsible for finding that limit rather than asking the user for it. The
Ollama catalog implemented this by reading `details.context_length` from
`/api/tags`, described in DR-006's provider notes as turning "DR-010's context
limit from an assumption into a measurement."

It was still an assumption. `/api/tags` reports the **model's architectural
maximum**, not the window the runtime will serve. Ollama sizes the served
window from `OLLAMA_CONTEXT_LENGTH`, which defaults to 4096 regardless of what
the model supports. On the development machine this meant AutoSE read 262,144,
reported "256k ctx" in `--doctor` and the status line, and budgeted against a
window 64× larger than the 4,096 tokens actually being served.

Three properties of the failure made it severe rather than merely wrong:

1. **It is silent.** An over-long request is not rejected. `llama-server` runs
   with `--context-shift`, so the window slides and the *earliest* messages are
   discarded. Measured directly: a 10,125-token request returned
   `prompt_tokens: 2051` with HTTP 200 and no warning.
2. **It destroys exactly the wrong thing.** The messages at the front are the
   system prompt — the tool instructions, the workspace path, and the
   `REQUIREMENTS STATUS` ledger format every staged workflow depends on. A
   stage that loses its ledger format cannot produce a parseable report, so the
   runner retries a stage that will fail identically.
3. **It cannot be fixed from the request side.** Ollama's OpenAI-compatible
   endpoint ignores `num_ctx` in every position — nested under `options` and at
   the top level were both measured returning the same truncated count. DR-006
   keeps inference on the OpenAI-compatible surface, so AutoSE cannot raise the
   window; it can only find out what the window is and stay inside it.

Ollama does expose the real number, but on a different endpoint: `/api/ps`
reports `context_length` per *loaded* model. That is the measurement DR-010
wanted.

The same class of bug is not unique to Ollama. Any runtime may serve a window
smaller than the model's maximum, and the OpenAI-compatible surface has no
field that distinguishes the two.

## Decision

**`ModelInfo.context_length` means the window the runtime will actually
serve.** The model's architectural maximum moves to a separate
`max_context_length` field, which is diagnostic only and must never be used as
a budget.

The Ollama catalog sources the served window in this order:

1. `/api/ps`, when the model is loaded — the measured, authoritative value.
2. Otherwise `min(OLLAMA_DEFAULT_SERVED_CONTEXT, max_context_length)`, where
   the constant is Ollama's own documented default of 4096.

Assuming the default when nothing is loaded is deliberate and conservative. It
under-uses a runtime started with a raised `OLLAMA_CONTEXT_LENGTH`, which
degrades to working-but-cautious and self-corrects the moment the model loads
and `/api/ps` can be read. The alternative failure — assuming the maximum — is
the one that silently breaks every request.

**A capped window is surfaced, with the command that raises it.** When
`max_context_length` exceeds `context_length`, session setup emits a warning
naming both numbers and quoting `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`. A
user cannot otherwise discover this: nothing in the runtime's behaviour
distinguishes a small window from a model that is answering badly.

**A runtime that truncates anyway is detected at run time.** After each round,
the agent loop compares the provider's reported `prompt_tokens` against the
estimated input. Below `TRUNCATION_WARNING_RATIO` (0.5) of the estimate, it
emits `context_truncated_by_runtime` once per call. This is provider-agnostic
and needs no runtime-specific knowledge, so it covers vLLM, LM Studio,
llama.cpp, and any hosted endpoint with the same trap. The threshold sits far
outside normal tokenizer drift: the local estimator deliberately runs high
(three characters per token against a real ~four), so losing half the request
is evidence, not noise.

## Consequences

### Positive

- The budget is computed against a number that reflects reality, so DR-010's
  fail-closed `ContextBudgetError` fires where it should instead of a request
  being silently mutilated by the runtime.
- The system prompt stops being silently evicted, which removes a whole class
  of "the model ignored its instructions" failures that were not the model's
  fault.
- The run-time reconciliation check catches the same failure on any provider,
  including ones AutoSE has no special knowledge of.
- `--doctor` stops reporting a capability the machine does not have.

### Negative

- Out of the box against a default Ollama install, AutoSE now budgets for 4,096
  tokens where it previously claimed 262,144. Real capability is unchanged —
  it was never more than 4,096 — but the reported figure drops sharply, and a
  user who does not read the warning will find AutoSE more conservative than
  they expect.
- An unloaded model on a runtime with a raised `OLLAMA_CONTEXT_LENGTH` is
  under-budgeted until it loads.
- `ModelInfo` carries two context fields that are easy to confuse; the wrong
  one being picked up in future code is a live risk.

### Mitigations

- The startup warning names both numbers and the exact command, so the
  conservative default is visible and one step from being raised.
- `max_context_length` is documented at its definition as display-only, and
  `is_context_capped` exists so callers ask a question rather than comparing
  the two fields themselves.
- Tests pin all three paths — measured, fallback, and the warning's content —
  so a regression to "read the maximum" fails the build.

## Alternatives considered

### Keep reading `/api/tags` and raise the window per request with `num_ctx`

Rejected because it does not work. Measured against Ollama 0.31.1: `num_ctx`
supplied to `/v1/chat/completions` both nested under `options` and at the top
level left `prompt_tokens` at the truncated 2,051. The OpenAI-compatible layer
does not map it.

### Switch the Ollama provider to the native `/api/chat`, where `num_ctx` works

Rejected for now, though it is the only option that raises actual capability
rather than reporting it honestly. It means a second chat implementation, a
second streaming and tool-call normalisation path, and a direct contradiction
of DR-006's finding that Ollama needs no inference-side special-casing — a
significant change that deserves its own DR and its own tests, not to be
smuggled in as part of a correctness fix. Worth revisiting: it would let
AutoSE give users the full window without asking them to configure a daemon.

### Treat an unknown served window as `None` and fall back to round-pruning

Rejected. `None` disables token budgeting entirely in favour of a fixed round
cap, which cannot know whether twenty rounds fit in 4,096 tokens — so the
silent truncation continues, just without a number attached. A conservative
concrete value is strictly better than no value.

### Probe the served window empirically at startup

Rejected as too expensive for the DR-015 constraint that discovery stay
snappy. Establishing the window by binary-searching request sizes costs several
generations on hardware where one generation can take minutes, and it would run
before the user has asked for anything.

### Only warn, and keep budgeting against the maximum

Rejected. It leaves the broken behaviour in place and delegates the fix to the
user noticing a warning. DR-010's premise is that the budget is calculated
correctly, not that the user is told it might not be.
