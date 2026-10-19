# DR-039: A Tool Call the Runtime Cannot Parse Is Retried, Not Fatal

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** `MalformedToolCallError` in `src/logic/errors.py`; its detection in `src/logic/inference/ollama_chat.py`; the bounded retry in `src/logic/common/agent.py`; the `tool_call_malformed` event and its line in the dashboard. Builds on DR-019 (nudge a silent turn instead of restarting the stage) and DR-035 (Ollama's native endpoint).

## Context

Running `craft` against `qwen3.6:35b-a3b` on Ollama, the Plan stage stopped
with:

> InferenceError: ollama: XML syntax error on line 3: element `<function>` closed by `</parameter>`

Qwen models write tool calls in an XML-like format that Ollama parses on the
server. The model had written one malformed call; Ollama's parser rejected it
and sent the error as a line of the response stream; the client raised it as an
`InferenceError`; and nothing between there and the stage caught it, so the
stage — and the turn — failed. The same request, sent again, succeeded: the
fault was in one generation, not in the request, the model or the connection.

The agent loop already treats the analogous failure on AutoSE's own side —
tool arguments that are not valid JSON — as something to report back to the
model and continue from. A parse failure on the runtime's side deserved the
same treatment and had none.

## Decision

- The Ollama client recognises its parser's errors (text naming an XML syntax
  error or a tool call) and raises `MalformedToolCallError`, a subclass of
  `InferenceError`, distinct from a connection failure or a context-length
  rejection.
- `BaseAgent` catches it around the model call. It adds a user message saying
  what the runtime could not parse and asking for the call again, emits
  `tool_call_malformed` (stage, round, attempt, the error), and runs the next
  round in the same call — keeping everything the stage has gathered, as DR-019
  does for a silent turn.
- At most three retries per stage call (`_MAX_MALFORMED_TOOL_CALLS`). A fourth
  malformed call re-raises: a model that keeps writing calls the runtime cannot
  read is not going to be talked out of it, and DR-009 forbids hiding that.
- The dashboard shows each retry as an activity line, so a run that recovered
  still says it had to.

## Consequences

### Positive

- A single malformed tool call no longer costs a whole turn.
- The failure that remains — repeated malformed calls — is a typed, reported
  error, distinguishable from an unreachable runtime.

### Negative

- Detection matches error text, which Ollama could change. A new wording would
  fall back to today's behaviour (the stage fails), not to anything worse.
- A retry costs a round of inference.

### Mitigations

- The matched phrases are one constant at the provider boundary, where DR-006
  says provider quirks belong, and a test pins the wording seen in practice.

## Alternatives considered

### Retry every `InferenceError`

Rejected. An unreachable server or a rejected key fails identically on every
retry; retrying it only delays the report, and hides the difference between a
bad generation and a broken setup.

### Restart the stage

Rejected for the reason DR-019 rejects it for a silent turn: the stage would
lose what it had read and likely repeat the same path.

### Parse the model's tool-call text in AutoSE instead of Ollama

Rejected. Ollama owns the model's chat template and its tool-call format;
re-implementing its parser would couple AutoSE to each model family's format.
