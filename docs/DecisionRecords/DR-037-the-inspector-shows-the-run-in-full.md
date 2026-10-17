# DR-037: The Dashboard Has an Inspector That Shows the Run in Full; the Conversation Stays Compact

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** `tool_called`/`tool_completed` event data in `src/logic/common/execution.py`; new `src/gui/static/inspector.js`; the shell layout, the work card and the inspector wiring in `src/gui/static/{index.html,app.css,app.js}`; the static allowlist in `src/gui/server.py`. Builds on DR-029 (no build step), DR-031 (several sessions), DR-033 (the live view survives a reload).

## Context

The dashboard drew a run in one place: the work card in the conversation. The
card had to be two things at once. As the conversation's account of a request
it should be a few lines. As the record of what the agent did it should be
complete: every file read, every command run, how long each took, what came
back. Doing both, it was neither. It showed the last seven activity lines,
collapsed the rest behind "show earlier steps", and still pushed the
conversation down by a screen while a stage ran. And what came back from a tool
was never shown anywhere: `tool_completed` carried only the result's length,
so finding out why a stage read the same file three times meant reading the
session file.

Kiro Crew splits these: a chat that stays short, and an activity view that
shows every step of every agent. The interaction is right for AutoSE too, and
AutoSE has something to put in it that Kiro does not: a staged pipeline, so a
step belongs to Plan, Execute or Validate.

## Decision

### The tool events carry what an inspector needs

`tool_called` and `tool_completed` both carry the call's `call_id`, so a result
is matched to its call without guessing. `tool_completed` adds:

- `preview`: the result the model was handed, up to 1,200 characters
  (`TOOL_PREVIEW_CHARS`). Bounded, so a large result costs the event stream
  little; the full text went to the model, and an oversized one is on disk as
  its artifact (DR-011).
- `failed`: whether the result is an error.
- `elapsed_ms`: how long the tool took.

These are `logic/` events, so the terminal and the JSON adapter get them too.

### An inspector beside the conversation

The shell gains a third column, the **inspector**, with tabs. Its first tab,
**Activity**, lists every step of the run: a row per tool call (verb, target,
duration, status) and per notable moment (an approval, a retry, a verdict),
grouped under the stage that produced it. A row expands to show the call's
arguments and the result preview. A summary line counts the tool calls and the
failures, and gives the run's duration once it is over; a ticking clock would
repaint the panel every second, and the card already has one.

The inspector describes the focused session's run in progress, else the last run
this page watched. A turn that finished before the page loaded has no step
record to show; its receipt in the conversation still does.

It is toggled from the header and remembered per browser. On a screen narrower
than 1,240 px it is a drawer over the conversation rather than a column.

### The work card is compact by default, and expands

The card shows its stages, the latest step and the streamed text, clamped.
**Details** expands it in place to the full activity list, the whole stage
output and the reasoning: the verbose card it was before, on request. This is
the user's choice of default: compact, with the detail one click away in the
card itself as well as in the inspector.

### `inspector.js`: renderers in their own file

The inspector's markup lives in `inspector.js`, a plain script loaded before
`app.js`: pure functions from run data to markup, which `app.js` calls when it
paints. It is the first split of a page script that had grown past 1,900 lines,
done without a build step or a module loader (DR-029). Later inspector tabs
belong in that file.

## Consequences

### Positive

- What a run did is visible step by step, with what each step got back, while it
  runs and after.
- The conversation reads as a conversation; the verbose card is one click away.
- The step record rides on events the server already replays to a reloaded page
  (DR-033), so a reload rebuilds the inspector too.

### Negative

- Each tool result adds up to 1,200 characters to the event stream and to a
  running turn's timeline.
- A finished turn's steps are not persisted, so a page opened afterwards shows
  the receipt but no step list.
- Two scripts share one global scope; a name defined in both would collide.

### Mitigations

- The preview is a constant in one place, and the timeline is already bounded
  (DR-033).
- Persisting the step record with the turn is a contained follow-up: the events
  that build it already exist.

## Alternatives considered

### Keep everything in the card and make it collapsible

Rejected as the only view. A collapsed card hides the record from someone who
wants it, and an expanded one buries the conversation. The card keeps a
Details toggle for in-place reading; the inspector is where the record lives.

### Fetch a step's full output on demand from the server

Rejected for now. It needs an endpoint that reads tool results back, which the
bridge does not keep, for little gain over a bounded preview plus the on-disk
artifact for oversized results.

### Split `app.js` into ES modules

Deferred. Converting the existing script to modules changes how every function
is reached; adding new renderers in a separate plain script gets the separation
for the new code today without that rewrite.
