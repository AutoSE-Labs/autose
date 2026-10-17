# DR-030: Every Cap Is Unlimited by Default, and the User Holds the Stop

- **Status:** Accepted
- **Date:** 2026-08-31
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/limits.py`, `src/logic/common/cancel.py`, `src/logic/tuning.py`, the agent loop, the staged runner, and every Adapter. **Amends DR-009 — the mechanism stands, the defaults invert. Does not supersede it.**

## Context

AutoSE shipped with a cap on nearly every loop it has: 30 tool-calling rounds
per agent call (40 for `forge`), 3 validation loopbacks, 2 format retries, a
one-hour wall clock, 20 edits per call, 200 output tokens for the classifier.
Each was a reasonable guess, and every one of them was sized — implicitly —
against the way a large hosted model works.

DR-015 commits AutoSE to local-first inference, which means the models it
actually runs against are small. A 7B model does the same job in a different
shape: it reads one file per round where a frontier model reads four, it
searches before it is sure what it is looking for, it gets a ledger format
wrong and fixes it when shown the parser's complaint. None of that is failure
— it is how the target model works — and all of it spends the exact resource
these caps meter.

The result was the failure mode a cap is supposed to prevent, inverted. A
`round_cap` stop did not catch a runaway loop; it interrupted correct work two
reads from finishing, threw away the context that work had gathered, and
reported a partial draft as "stopped early". A three-attempt format cap failed
whole requests over a heading a fourth attempt would have got right. The caps
were not protecting the user from the model; they were protecting the model
from finishing.

DR-009 anticipated part of this. It considered "let the workflow run until a
human manually cancels it" and rejected it **as the only safeguard**, on the
grounds that an unattended run could otherwise consume unbounded time. That
reasoning is sound and is not being reversed. What has changed is the
evidence about which default is right: for an attended, local, interactive run
— which is what AutoSE is — the number that stops the work is far more often
wrong than the person watching it.

There was also no stop to hold. `ctrl-c` raised `KeyboardInterrupt` through the
turn, which discarded the draft, the finished stages' artifacts, and the
summary of what had already been written to disk. The dashboard had nothing at
all: a turn runs on a worker thread, and the browser could only wait.

## Decision

### 1. `-1` means unlimited, in one place

`src/logic/limits.py` owns the convention: a config file writes `-1`, which
normalises to `None` internally, which is what enforcing code branches on. Any
negative value reads the same way, so `-1` is a convention to document rather
than a sentinel to match exactly. `counted(limit)` gives a loop that either
stops at a count or never stops, so a caller writes one `for` either way.
`describe_limit` renders a cap for a person as a number or as the word
"unlimited", so no user-facing string ever prints `-1`.

Zero is treated as unlimited for a *duration* (a budget of no seconds is not a
budget, it is a run that could never start) and clamped up to the minimum for a
*count* (a stage allowed no attempts cannot answer).

### 2. The caps that decide *how long AutoSE works* default to unlimited

`max_tool_rounds`, `max_validation_iterations`, `max_format_retries` and
`time_budget_seconds` are `-1` for every tier. `max_edits_per_call` and the
classifier's `max_output_tokens` accept `-1` and keep small defaults, since
they refuse work rather than end it.

Every one stays configurable per tier, which is the point of leaving the
`tuning:` sections in place: "cap `spark` at 10 rounds, let `forge` run all
night" is a config file, not a code change. An unattended context — CI, a cron
job, a shared runner — is expected to put numbers back, and the example config
says so where the numbers are.

### 3. The caps that decide *what fits in one request* keep real defaults

The context budget (DR-010/DR-022), tool-output bounding (DR-011), session
memory (DR-004) and `command_timeout_seconds` are unchanged, and each accepts
`-1` for a repo that knows better. This is the line that makes the decision
coherent: those caps are not judgements about when to give up, they are
physical constraints of the model being served. Turning off tool-output
bounding does not let a small model work longer, it overflows the window it
was working in.

Two are called out because turning them off is genuinely risky, and the code
says so where it is set:

- `context.max_history_rounds` only applies when the served window is
  *unknown*. With nothing to measure against, a runtime that silently
  truncates from the front takes the system prompt with it (DR-022), and this
  cap is the only thing in the way.
- `shell.command_timeout_seconds` is the one place a stop cannot reach:
  nothing checks the cancel token while a subprocess blocks.

### 4. A cooperative stop, held by whoever is driving

`logic.common.cancel.CancelToken` is a thread-safe, resettable flag with a
reason attached. A session owns one for its lifetime and clears it at the start
of every turn. The agent loop checks it before each round and between tool
calls; the staged runner checks it between stages and between stage attempts.
Every check is at a point where no inference is in flight, so stopping costs
nothing and everything already produced is intact.

Stopping unwinds through `logic.errors.RunCancelled` and produces a
`stop_reason` of `"cancelled"` carrying the partial answer, the artifacts each
finished stage wrote, the changed-file list, and the change journal `/undo`
reverts from (DR-024). Per DR-009 it is **not** a success — `succeeded` stays
false and the JSON adapter still exits non-zero — but it is reported as the
user's choice rather than as a fault.

Cooperative rather than an exception thrown from outside, for three reasons:
what has been done stays done; one mechanism serves a signal handler, an HTTP
request on another thread, and a test calling `request()`; and a
`threading.Event` is visible across threads, which is the only way the
dashboard's request reaches a turn already running on a worker.

### 5. Every Adapter offers it

- **Terminal:** the first `ctrl-c` asks the session to stop and renders the
  partial outcome; a second aborts as before. Two stages, because with no caps
  left `ctrl-c` is now the normal way to end a long run, not an escape hatch.
- **Dashboard:** two Stop controls, both posting to `/api/interrupt` — one on
  the live card's head strip, beside the elapsed clock, because that is what
  the user is looking at when they decide they have seen enough; and one in the
  composer, standing *in the send button's place* while a turn runs, because a
  long run's card scrolls out of view and the composer does not. Neither sits
  next to a disabled "Working…" button, which is what made the first attempt
  unfindable. The pending state survives a reconnect, so a reopened tab does
  not offer a button that has already been pressed.

  This also fixed a bug it exposed: the page learned a turn had started from
  the `busy` frame but never wrote it to `state.running`, and the server only
  broadcasts a full state frame when a turn *ends*. So every control that reads
  `state.running` — the composer, the tier and model pickers, the presence
  label, and the new Stop button — saw `false` for the whole run.
- **JSON (DR-027):** `SIGINT` asks the turn to stop, so a piped run still emits
  a well-formed `result` line a script can act on.

## Consequences

### Positive

- The models AutoSE is built for get to finish. A tool loop past 30 rounds, or
  a fourth attempt at a ledger, is no longer an artificial failure.
- Stopping keeps the work. A user who ends a long run gets the draft, the
  artifacts, the file list and `/undo` — which is strictly more than the old
  `KeyboardInterrupt` left them with.
- One convention, one module. "Is this configured value a bound or does it mean
  don't stop?" is answered in `limits.py` rather than at a dozen call sites.
- The bounds are still there, still tested, still per tier. Restoring the old
  behaviour is a config file, and an unattended deployment can do exactly that.

### Negative

- **An unattended run can now run indefinitely.** This is the real cost, and it
  is not mitigated in code by design — it is mitigated by configuration and by
  saying so plainly in the example config. A CI job that does not set bounds
  can burn a runner.
- A genuinely stuck model — one that returns an empty turn or a malformed
  ledger forever — now loops until someone stops it, where before it failed on
  its own. The nudge cap (DR-019) stays bounded at 2 for this reason, but it
  only covers silence, not a persistently wrong format.
- Every enforcement site now branches on `int | None` instead of `int`. The
  normalising helpers keep that in one shape, but it is more type surface than
  a plain integer cap.
- `stop_reason` gains a value every Adapter has to render, and a run that ends
  well can now end in four distinct not-a-success ways.

### Mitigations

- The example config leads with the `-1` convention, says what stops a run
  instead, and tells an unattended deployment to put numbers back.
- `WorkflowBounds.unlimited` and the `workflow_started` event report the
  effective bounds as normalised values (`null` for unlimited) rather than the
  sentinel, so an Adapter never has to know about `-1`.
- Elapsed time is still measured and reported when there is no time budget, so
  a long run is visible even though it is not bounded.

## Alternatives considered

### Raise the caps instead of removing them

Rejected. It moves the number without fixing the problem: any figure large
enough for a 3B model reading a large repository is large enough that hitting
it is a pathology, not a budget — at which point it is not doing the work a cap
is for, and it still cuts off the one task that legitimately needs more.

### Keep the caps and make them adaptive (scale by model size, window, or observed rate)

Rejected as speculative. It needs a model of how much work a request should
take, which AutoSE does not have and cannot get cheaply, and it would fail in
the same direction — an unusual task looks like a runaway loop to any
heuristic. A person watching is a better judge and is already present.

### Keep `KeyboardInterrupt` as the stop and skip the token

Rejected. It cannot reach the dashboard's worker thread at all, and it
guarantees the loss of exactly what the user wanted to see: an interrupt
arriving mid-write leaves no reliable partial outcome, no artifacts, and no
journal to revert.

### Make every cap unlimited, including the context and tool-output bounds

Rejected. Those caps exist because a served context window is finite, and
DR-022 records what happens when a request exceeds one: some runtimes truncate
from the front instead of refusing, silently dropping the system prompt. That
is a failure the user cannot see, which is worse than a cap they can configure.

### Add a per-run `--max-rounds` flag instead of config

Rejected as the primary surface, not as an idea. The request this decision came
from was explicitly "anyone who wants it should only need to edit the config",
and a flag would have to be passed on every invocation to have the same effect.
DR-021 already makes `.autose/config.yaml` the place a repo's behaviour is
committed and shared.
