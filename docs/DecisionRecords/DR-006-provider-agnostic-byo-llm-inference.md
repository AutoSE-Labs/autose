# DR-006: Provider-Agnostic, Bring-Your-Own-LLM Inference

- **Status:** Accepted
- **Date:** 2026-08-29
- **Decision owners:** AutoSE maintainers
- **Scope:** Model/provider integration, product positioning

## Context

Most consumer coding-agent products are tied to a single model vendor, which is a real constraint for users who need to run against a self-hosted, local, or otherwise private inference endpoint (compliance requirements, air-gapped environments, cost control, or simply preferring a specific model). Building the product against one vendor's API also creates a dependency risk for the product itself.

## Decision

The product will never provide, bundle, or proxy a model. Instead, it targets **any OpenAI-compatible chat-completions endpoint** — self-hosted runtimes (e.g. Ollama, vLLM, LM Studio) and hosted providers (e.g. OpenAI-compatible commercial APIs) alike — configured via a base URL, optional API key, and model name.

All provider-facing code (classifier calls, agent calls, knowledge retrieval calls) is written against the OpenAI-compatible chat-completions request/response shape only, with no vendor-specific SDK or feature dependency assumed to be available. Any provider-specific quirks (e.g. differing tool-call formats) are handled at the integration boundary, not allowed to leak into `src/logic/`'s core agent/workflow code.

## Consequences

### Positive

- Users can run entirely on local/self-hosted infrastructure with no data leaving their environment, if they choose a local endpoint.
- No vendor lock-in for the product itself; a user can switch models/providers by changing configuration only.
- Differentiates against vendor-locked competitors for privacy-sensitive, cost-sensitive, or offline-capable use cases.

### Negative

- Cannot rely on vendor-specific capabilities (e.g. a specific provider's native structured-output guarantees) as a hard dependency; must degrade gracefully across providers with varying capability levels.
- Smaller/local models have weaker instruction-following and tool-call reliability than frontier hosted models; prompt and tool design (see DR-007) must account for this rather than assuming a top-tier model.

## Alternatives considered

### Integrate deeply against one vendor's API and SDK

Rejected. Maximizes short-term feature access to that vendor's specific capabilities at the cost of vendor lock-in for the product and exclusion of self-hosted/local-model users entirely.

### Support multiple vendors via per-vendor SDK integrations

Rejected as the primary approach. Multiplies integration and maintenance surface; standardizing on the widely-implemented OpenAI-compatible chat-completions shape covers the large majority of both hosted and self-hosted options with one integration path.
