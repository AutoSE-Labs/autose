# DR-034: A Served Context Window Gets a Verdict and a Computed Remedy, Not a Fixed Warning

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/inference/context_advice.py`; `ModelInfo.gpu_fraction` and its source in `src/logic/inference/ollama.py`; the warnings in `src/logic/session.py` and `src/logic/diagnostics.py`; the dashboard's CTX cell. **Refines DR-022**, which it does not supersede: the gap is still detected the same way.

## Context

DR-022 made the gap between a model's maximum window and the window the
runtime serves visible, because a runtime that serves less trims requests
silently. It reported every such gap the same way: a standing warning with the
remedy `` `OLLAMA_CONTEXT_LENGTH=32768 ollama serve` ``.

That stopped being useful the moment someone followed it. On a laptop with a
6 GB RTX 2060, Ollama was raised from its 4,096 default to 32,768 for
`qwen3.5:9b`, a 262,144-token model. The dashboard then showed, permanently and
in the colour of an error, that the model "supports 262,144 tokens but the
runtime is serving only 32,768", and told the user to fix it by setting the
value they had just set. It asked "what is this and what can I do about it?"
The honest answer was "nothing; it is the right setting", and the interface
should have been the one to say so.

Two facts the warning ignored make that answer knowable:

- **AutoSE's own needs.** Below `min_usable_context_tokens` (8,192) a session
  refuses to run. Above roughly 32k, every `craft` and `forge` stage has room to
  read and answer; more buys longer runs, not correctness.
- **The machine.** Ollama's `/api/ps` reports `size_vram` beside `size`. That
  model was 58% on the GPU. A larger window grows the KV cache on the same card
  and pushes more of the model onto the CPU: on that machine, raising the
  window makes AutoSE slower, not better.

## Decision

### One verdict per model, from one pure function

`advise_context(info, provider=, min_usable=)` returns a `ContextAdvice`: a
level, the served and maximum windows, a recommended window (or none), a
one-line summary, the reasoning, and a remedy.

| Level | When | Recommended | Remedy |
|---|---|---|---|
| `blocking` | served < `min_usable` | `min(32k, model max)` | yes |
| `warning` | served < 32k, and the model supports more | `min(32k, model max)` | yes |
| `info` | served ≥ 32k but < model max, model partly on CPU | keep it | none |
| `info` | served ≥ 32k but < model max, model fully on GPU | double, capped at max | optional |
| `ok` | serving the model's full window, or unknown | — | none |

It does no I/O, so the terminal, `--doctor` and the dashboard show the same
verdict, and the rules are pinned by plain unit tests.

### The remedy is computed, and never the value already in use

The remedy is built from the recommended window and a per-runtime template
(Ollama's environment variable, vLLM's `--max-model-len`, llama.cpp's `-c`, LM
Studio's load setting). A runtime with no template gets the target size and no
command, rather than an Ollama variable on a server that is not Ollama.

### Only what hurts is a standing warning

`_capability_warnings` and `--doctor`'s advisories now report a window only at
`warning` or `blocking`. A deliberate cap is no longer a warning: it is shown as
the dashboard's CTX cell, whose mark carries the level (calm for `ok`/`info`,
signal colours for `warning`/`blocking`). Clicking the cell opens the
reasoning, the three numbers, and a copyable command when there is one.

### `ModelInfo.gpu_fraction`

`size_vram / size` from `/api/ps`, for a loaded Ollama model; `None` for a cold
model or a runtime that does not report it.

## Consequences

### Positive

- The setting a user chose on purpose stops reading as a fault.
- Every remedy is specific to the model, the runtime and the machine, and is
  never a no-op.
- A user on a small GPU is told not to raise the window, which is the advice
  that actually makes their runs faster.

### Negative

- `gpu_fraction` exists only for a model that is loaded. The first verdict on a
  cold server cannot know about CPU spill-over, so it can suggest more room that
  the machine would not handle well.
- The 32k threshold is a judgement about today's stage prompts and tools, not a
  measurement. If prompts grow, it has to move.

### Mitigations

- Re-reading the runtime (`/api/reload`, or restarting the dashboard) runs
  discovery again and picks up `gpu_fraction` once the model has loaded.
- `COMFORTABLE_CONTEXT_TOKENS` is one named constant in `context_advice.py`.

## Alternatives considered

### Keep the warning and only fix the example value

Rejected. A correct number in a warning that should not be shown is still a
warning that should not be shown, and the user would still be told to act on a
setting that is right.

### Let the user dismiss the warning

Rejected as the main fix. Dismissal teaches people to dismiss, including the
`blocking` case that really does stop a run. A level that is right in the first
place needs no dismissal.

### Raise the window automatically

Rejected. The window is a property of how the user's model server was
started, which AutoSE does not own and, for a remote runtime, cannot reach.
AutoSE advises; the user decides.
