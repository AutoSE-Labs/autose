# DR-027: A Non-Interactive JSON Adapter, and What It Proves About DR-002

- **Status:** Accepted
- **Date:** 2026-08-30
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/tui/headless.py`, `autose --json`. **Exercises DR-002; inherits DR-009 and DR-012.**

## Context

AutoSE could only be driven by a human at a terminal. `--yes` existed for
unattended approval, but the output was still Rich-rendered prose: panels,
colour, spinners, a streamed answer. A CI job or a script wanting to run
AutoSE had to either parse that or not use it.

There is a second, less obvious reason to build this. DR-002's central claim
is that `src/tui/` is a *thin* adapter — that every decision lives in
`logic.session.Session`, and swapping the interface changes nothing about
behaviour. That claim had never been tested, because there was only one
adapter. `tests/test_architecture.py` checks the *dependency direction*, which
is necessary and not sufficient: an adapter can respect the import boundary
and still be the only place that knows something important.

Writing a second adapter is the test. It found real gaps — the changed-file
list `spark` never produced, the exit code that had nowhere to come from —
and it will keep finding them, which matters more for the GUI (DR-013) than
for any script: the GUI is meant to be the *only* difference between editions,
and that is a much stronger claim than "it compiles".

## Decision

`autose --json "<prompt>"` runs one prompt with no interactive interface and
writes **newline-delimited JSON** to stdout.

### One object per line, streamed

Every line is a complete JSON object, flushed as it is written, so a consumer
processes the stream as it arrives rather than waiting for a document to
close. The shape is:

- one `started` object naming the version, workspace, prompt, and approval
  posture;
- zero or more `event` objects, each wrapping a `logic` event verbatim —
  stages, tools, approvals, classification, context warnings;
- zero or more `warning` objects;
- exactly one terminal `result` or `error`.

Events pass through as data rather than being interpreted. An adapter that
decided which events mattered would be making a decision, which is the thing
this module exists to demonstrate it does not do.

### The exit code carries the outcome

`0` when the turn completed, `1` when it did not — a bound was hit, a stage
failed, no runtime answered — and `2` when AutoSE could not be configured or
started. DR-009 forbids presenting an exhausted budget as success; in a shell
that rule is an exit code, not prose a script would have to pattern-match.

### Unattended means fail-closed

The session is constructed with **no approval gate**, so DR-012's
`DenyingApprovalGate` applies: a shell or network call in an unattended run is
refused, and the refusal is visible in the stream as an `approval_resolved`
event. `--yes` remains the deliberate opt-out, and it is attributed — the
authorising identity is recorded exactly as it is interactively, because an
unattended run is where an unattributed auto-approval would matter most.

### Changes are reported as diffs, not as content

A `result` names each changed file with its kind, its line counts, and a
unified diff. It does **not** carry the raw before/after bytes DR-024 keeps
for undo. A consumer wants to know what happened, and a diff says that in a
form both a human and a patch tool can read; putting whole file contents on
stdout is a different feature with different consequences.

### Sessions work the same way

A headless run stores its session (DR-026) and accepts `--resume`, so a
scripted run and an interactive one are the same kind of thing and can hand
work to each other.

## Consequences

### Positive

- AutoSE is scriptable: a CI job can run it, read the result, and branch on
  the exit code.
- DR-002's claim is now executable. A behaviour that lives only in the TUI
  shows up as a gap in the JSON output.
- Building it found two real defects: `spark` never reported the files it
  changed, and there was no non-prose way to distinguish a completed run from
  one that hit a bound.
- A GUI (DR-013) inherits a facade that has been proven adequate for a second
  consumer.

### Negative

- A second adapter is a second thing to keep current. An event added for the
  TUI's benefit will reach the JSON stream whether or not that was intended,
  which is mostly good and occasionally noise.
- Newline-delimited JSON is not a single JSON document, so a consumer using a
  whole-file parser has to be told. The alternative is worse.
- Streamed answer text is not forwarded token by token; the answer arrives
  complete in the `result`. A consumer wanting live text would need a further
  decision about how to frame it.

### Mitigations

- The writer catches **everything** when serialising, not just
  `TypeError`/`ValueError`: `default=str` invokes an object's own `__str__`,
  which is arbitrary code and can raise anything. A tool result with a hostile
  repr degrades to a `warning` line that is still valid JSON, rather than
  killing the stream. This was found by a test, not by review.

## Alternatives considered

### One JSON document written at the end

Rejected. A local model run takes minutes; a consumer that learns nothing
until it finishes cannot show progress, and a run that is killed leaves
nothing at all. Line-delimited output survives both.

### A `--quiet` mode that prints only the answer

Rejected as the primary form, and it remains a reasonable future addition.
It cannot carry which files changed, how the run ended, or why a tool was
refused — all of which a script has more use for than the prose does.

### Put the adapter in `src/logic/`

Rejected outright by DR-002. Serialising for a consumer is presentation, and
`logic` must not acquire an opinion about output formats. It lives in
`src/tui/` alongside the terminal adapter because both are the same kind of
thing: a way of driving `Session` from a command line.

### Reuse the TUI's event rendering with colour disabled

Rejected. Rich's plain output is still designed for human reading — wrapped,
padded, aligned — and parsing it would be screen-scraping with extra steps.

### Emit whole file contents for each change

Rejected. It makes the output size unbounded in a way a diff is not, and the
information a consumer actually wants ("what changed") is what a diff already
says. The bytes exist in the stored session (DR-026) for the consumer that
genuinely needs them.
