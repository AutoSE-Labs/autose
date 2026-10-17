# DR-015: Local Inference Is the Default Target; Hosted APIs Are Optional

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/inference/`, runtime configuration defaults, first-run experience. **Refines DR-006 — does not supersede it.**

## Context

DR-006 decided that AutoSE speaks only the OpenAI-compatible chat-completions shape, and treated self-hosted runtimes and hosted commercial APIs as equally-supported endpoints behind one integration. That decision stands. What it deliberately left open is which of the two the product is actually *designed around* — and "supported symmetrically" turns out not to be a real position, because the two have materially different failure modes and the defaults can only favour one:

- A hosted API is reachable, has a documented context window, reliably supports tool calling, and needs a key. A local runtime may not be running at all, serves whatever models the user happens to have pulled, publishes its own real context length, and may serve a model that cannot tool-call.
- Configuration that is right for a hosted provider is wrong for a local one. An API key is mandatory there and pointless here. A 128k context window is a safe assumption there and wildly wrong for a 4k local model. A short request timeout is correct there and guarantees failure on slow local hardware.
- "Configure a base URL, an API key, and a model name before anything works" is an acceptable first run for a hosted API. For a user who already has Ollama running with models pulled, it is pure friction — every value the product is asking for is one it could have discovered.

Current product direction is that AutoSE is **for** people running models on their own machines: privacy-sensitive work, air-gapped environments, cost control, and offline capability. Hosted endpoints remain fully supported, but they are the configured-on-purpose case, not the shape the defaults, diagnostics, and first-run flow are built for. Leaving that unstated means every future default gets argued from scratch, and the hosted assumptions — which are the conventional ones, and therefore the ones a contributor reaches for by habit — quietly win.

## Decision

Local inference is the **default and primary target**. Hosted OpenAI-compatible endpoints stay supported, as an explicit opt-in via configuration.

Concretely:

### Defaults assume a local runtime

With no configuration at all, AutoSE targets a local runtime on its conventional port. No API key is required or requested; an empty key is the normal case, not a degraded one. Request timeouts default to something a slow local model on weak hardware can actually meet, rather than a value tuned for a hosted API's latency.

### Runtimes and models are discovered, not demanded

AutoSE probes the conventional local endpoints (Ollama, LM Studio, vLLM, llama.cpp, and similar) in parallel with a short timeout, and reports which are reachable and what each serves. A user with a runtime already running is never asked to type a base URL, and a user with none gets a diagnostic naming what was probed rather than a connection error.

Model enumeration is a Strategy seam (`ModelCatalog`) alongside `ChatModel`, because runtimes differ in what they can tell us: the OpenAI-compatible `/v1/models` returns bare identifiers, while a native endpoint may return far more. A provider registers both.

### Discovered facts beat assumed ones

Where a runtime publishes a model's real properties, those are used in preference to any default:

- **Context length.** DR-010 requires a calibrated *estimate* of how many tokens a request costs, because no provider-independent tokenizer exists. It says nothing about the *limit* being unknowable — and a runtime that publishes the model's real context length makes that limit exact. Discovery therefore feeds DR-010's budget a true limit; only the estimate remains heuristic.
- **Tool-call support.** DR-006 already notes that smaller local models tool-call unreliably. A runtime that publishes per-model capabilities turns that from a runtime surprise into a pre-flight check: selecting a model that cannot tool-call is reported before a stage is started, not discovered when a stage silently produces nothing.

### Output reservation scales to the window it is reserved from

A fixed reserved-output figure sized for a large hosted context window consumes an entire small local one, making DR-010's budget refuse every request against a 4k model. When not set explicitly, reserved output is derived as a fraction of the model's actual context limit, within fixed floor and ceiling bounds.

### The wire protocol does not change

Everything above is discovery, defaults, and diagnostics. Every actual inference call still goes through the single OpenAI-compatible chat-completions shape DR-006 mandates, through the same `ChatModel` interface. A native endpoint may be used to *enumerate* models and read their properties; it is never used to run one. A provider that offers nothing beyond the OpenAI-compatible surface remains a first-class provider with a bare catalog.

## Consequences

### Positive

- Zero-configuration first run for the target user: a running local runtime is found, its models listed, and one selected, with nothing typed.
- The context budget operates on a real limit rather than a guess wherever a runtime publishes one, removing a whole class of overflow failure DR-010 could otherwise only mitigate with margin.
- A model that cannot tool-call is caught before work starts, rather than manifesting as a stage that mysteriously accomplishes nothing.
- Small-context models become genuinely usable, because reserved output scales to the window instead of swallowing it.
- The privacy claim becomes the default behaviour rather than a configuration a user has to get right: out of the box, nothing leaves the machine.

### Negative

- Probing local ports at startup is a small amount of work AutoSE does before it is asked to do anything, and touches ports it does not own. It is bounded by a short timeout and reports rather than acts, but it is not nothing.
- Two shapes of provider integration now exist — those with a rich native catalog and those with only `/v1/models` — so the experience is better on some runtimes than others. This is an honest reflection of what the runtimes actually expose, but it is a difference users will notice.
- Hosted providers become the less-exercised path, and will need deliberate testing to avoid regressing.
- Discovered values are trusted: a runtime that misreports a model's context length will mislead the budget more confidently than the previous guess did.

### Mitigations

- Every discovered value remains overridable by explicit configuration, so a misreporting runtime is a configuration fix, not a wall.
- Discovery failures are never fatal: an unreachable probe is reported and skipped, and explicit configuration bypasses probing entirely.

## Alternatives considered

### Leave DR-006's symmetry in place and let defaults be decided ad hoc

Rejected. "Supported symmetrically" is not implementable — the defaults for timeout, API key, context window, and first-run flow have to favour one case, and leaving that unstated means the conventional hosted-API assumptions win by habit rather than by decision.

### Integrate a local runtime's native API for inference as well as discovery

Rejected. It would fragment the agent loop across two request shapes for no capability gain — the runtimes in question already serve an OpenAI-compatible endpoint — and would contradict DR-006's single-integration decision. Native endpoints are used only for what the OpenAI-compatible surface genuinely cannot express: enumerating models and reading their properties.

### Bundle or auto-install a runtime and a model to guarantee a working first run

Rejected, and note DR-006 forbids it outright: the product never provides, bundles, or proxies a model. Discovering what the user already runs is the whole point; shipping our own would make AutoSE a distribution channel for someone else's weights and put us in the business of hosting them.

### Keep a fixed reserved-output default and let small-context models fail

Rejected. It makes DR-010's budget refuse every request against exactly the models this decision targets, which would reduce local-first to a slogan.

### Ask the user for a base URL and model on first run, as before

Rejected as the default. It asks for values the product can discover, and it asks them of the user least likely to need to be asked — someone who already has a runtime running. Manual configuration remains available and is required for hosted endpoints.
