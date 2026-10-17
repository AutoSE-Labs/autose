# DR-035: AutoSE Chooses the Context Window Where the Runtime Allows It, and the User Can Change It

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/inference/ollama_chat.py` (Ollama's native chat endpoint as a `ChatModel`); `Provider.serves_requested_window` in `src/logic/inference/registry.py`; `adopt_context_window`, `Session.set_context_window` and `Session.refresh_model_info` in `src/logic/session.py`; `window_choices` in `src/logic/inference/context_advice.py`; `--doctor`'s window advisories; the dashboard's context panel and `POST /api/context`. **Refines DR-006, DR-010, DR-022 and DR-034**; supersedes none.

## Context

DR-022 and DR-034 treat the context window as something the runtime decides and
AutoSE can only report on. For Ollama that was true only because of how AutoSE
talked to it. Its OpenAI-compatible `/v1/chat/completions` ignores `num_ctx`
(an [upstream pull request](https://github.com/ollama/ollama/pull/16825) to
change that was still open when this was written), so the window is whatever
`OLLAMA_CONTEXT_LENGTH` was when the server started: 4,096 by default.

That produced three failures, all seen in one afternoon of real use:

- **A guess shown as a fact.** A model Ollama has not loaded has no reported
  window, so AutoSE assumed the 4,096 default. A server started with
  `OLLAMA_CONTEXT_LENGTH=32768` was reported as "4,096 — below the 8,192 AutoSE
  needs", with a remedy to set the value already set.
- **A remedy the user cannot act on.** "Restart the server with an environment
  variable" is awkward on a laptop (a systemd unit) and impossible on a shared
  GPU node behind a scheduler, which is where the larger models run.
- **No way to try a size.** Whether 64k is worth it on a given machine is an
  empirical question. Answering it meant a restart per guess.

Ollama's native `/api/chat` takes the window per request (`options.num_ctx`),
and reloads the model at that size when it differs.

## Decision

### Talk to Ollama natively

`OllamaChatModel` implements `ChatModel` (DR-006) over `/api/chat`. Every
request carries `options.num_ctx` equal to the `context_limit` AutoSE budgets
against (DR-010), so the window budgeted for and the window served are the same
number by construction. Messages are translated at the boundary: native tool-call
arguments are objects, a tool result names its tool (`tool_name`) rather than a
call id, the stream is newline-delimited JSON, and usage is
`prompt_eval_count`/`eval_count`. `reasoning_effort: "none"` (DR-020) becomes
`think: false`. Nothing above the inference package sees the difference.

It subclasses the OpenAI-compatible client for authentication and HTTP error
handling only. Every other runtime keeps the OpenAI-compatible client.

### A runtime declares that it serves the requested window

`Provider.serves_requested_window` is `True` for Ollama and `False` elsewhere.
Code that needs to know asks the registry (`serves_requested_window()`), never
compares a provider name, so a future runtime that honours a per-request window
opts in by registering, with no edit to callers (DR-002).

### AutoSE chooses the window

`adopt_context_window()` is the one place a session's window is decided. For a
runtime that serves what it is asked:

1. a configured `inference.context_limit` wins;
2. else the window the runtime reports, when that is at least 32k
   (`COMFORTABLE_CONTEXT_TOKENS`, DR-034);
3. else 32k;

always capped at the model's maximum. The session's `ModelInfo` then describes
the window that **will** be served, marked as known. A GPU share measured at a
different window is dropped: it says nothing about the new one.

For any other runtime the old rule stands: the configured limit, else the
reported window.

### The user can change it, from the dashboard

`Session.set_context_window(tokens)` switches the window from the next request.
It refuses a runtime that cannot honour it (rather than pretending, DR-022), a
size below `min_usable_context_tokens`, and a size above the model's maximum.
It emits `context_window_changed`. `Session.refresh_model_info()` re-reads the
catalog after a turn, which is when the GPU share at the new size first becomes
knowable; the dashboard calls it after every turn.

`window_choices()` offers the powers of two from AutoSE's minimum to the model's
maximum, each with a note about what it means here: the minimum, tight, room for
every stage, or more room at a cost that depends on whether the model already
spills to the CPU. The dashboard's context panel shows them, marks the one in use
and the suggested one, and applies a choice through `POST /api/context`. It is
refused while that session is running a turn.

### Remedies say what actually works

Where AutoSE chooses the window, DR-034's remedy is AutoSE's own setting, as a
YAML snippet for `.autose/config.yaml`, never a server restart that would change
nothing AutoSE sends. `--doctor` no longer advises raising an Ollama server's
start-up window, for the same reason.

## Consequences

### Positive

- Ollama runs in a usable window out of the box, on a laptop or a remote node,
  with no environment variable and no restart.
- The 4,096 guess is gone for Ollama: the window is the one AutoSE asked for.
- Trying a size is one click; the next request uses it and the panel shows how
  much of the model landed on the GPU afterwards.

### Negative

- AutoSE now depends on Ollama's native API shape, not only the OpenAI one. A
  change there would need a change here.
- Changing the window makes Ollama reload the model at the next request: a few
  seconds on a GPU, longer on a CPU.
- A 32k default on a small GPU can push part of a model onto the CPU where
  Ollama's 4k default would not have. DR-034's advice says so, and the panel
  offers smaller sizes; 4k itself is below what AutoSE can work in.
- The chosen size is per session and not yet saved for the workspace; the panel
  shows the snippet that does that.

### Mitigations

- The native client is covered by tests that pin the request shape and the
  translation both ways; a real-model run verifies the path end to end.
- Every refusal (`set_context_window`) is a typed error with the reason.

## Alternatives considered

### Keep the OpenAI-compatible endpoint and send `num_ctx` anyway

Rejected. It is ignored, so the budget would claim a window the runtime is not
serving: precisely the silent truncation DR-022 exists to prevent.

### Warm the model up natively, then use the OpenAI-compatible endpoint

Rejected. A request without `num_ctx` carries the server default, and Ollama
reloads a model whose options differ, so the next OpenAI-compatible request
would undo the warm-up.

### Have the dashboard write the server's environment and restart it

Rejected. It works only for a local server AutoSE has the rights to restart,
couples AutoSE to how each user's Ollama is supervised, and cannot reach a
server on a GPU node.

### Wait for the upstream change

Rejected as the plan, kept as a possibility. If `/v1` starts honouring
`num_ctx`, `OllamaChatModel` could be retired and the flag kept; the rest of this
record would stand.
