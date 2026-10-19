# DR-033: The Dashboard's Live View Survives a Reload, and Shows Usage as It Is Spent

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/gui/bridge.py`, `src/gui/static/app.js`, and one new event, `round_usage`, in `src/logic/common/agent.py`. Builds on DR-029 (the dashboard streams events over SSE) and DR-031 (several sessions, each with its own turn).

## Context

Watching a run against a local 9B model, two things in the dashboard were
wrong in a way that read as "AutoSE has stalled":

- **A page opened mid-turn showed a bare card.** The bridge replays frames
  from `?since=` the snapshot's sequence, which is right for a reconnect, but
  the snapshot itself carried only the prompt and the tier. Everything the
  stream had already delivered — the stage list, the tool calls, the approval
  — was gone. Reloading the tab, opening a second one, or focusing a session
  that started elsewhere all produced a card that said "Thinking" with no
  stages and no activity, two minutes into a run. The stream *did* have the
  events: 63 output deltas, four tool calls and an approval were sitting in
  the bridge's replay buffer, unread.
- **Tokens read 0 for the whole of a stage.** Usage reached adapters only in
  `stage_finished` and `turn_finished`. A plan stage on a slow model runs for
  minutes; for all of that time the status bar said nothing had been spent and
  the context meter was empty.

Replaying the bridge's frame buffer from the turn's start was the obvious fix
and the wrong one. The buffer holds 600 frames, and one short run produced 246
reasoning deltas and 63 output deltas against four tool calls: a long turn
pushes its own `workflow_started` — the frame that names the stages — out of the
buffer long before it ends.

## Decision

### The snapshot carries the turn so far

Each open session keeps a bounded **timeline** of its running turn: every event
except the two streaming deltas (`agent_reasoning_delta`, `stage_output_delta`),
up to 400 entries. When full, the oldest entry after the first is dropped, so
`workflow_started` survives. The deltas are kept as a 6,000-character **tail**
of the current stage's output and of the reasoning. All of it resets when the
next turn starts.

The focused session's `current` in `/api/state` carries `timeline`,
`narration` and `thinking`. The rail's per-session summaries do not, so six open
sessions do not put six timelines on the socket.

### Replay and stream never overlap

An event is recorded into the timeline and published to the bus under one
lock. The snapshot reads the timeline and the bus sequence under that same lock.
So every event in a snapshot's timeline is at or before the sequence a page
streams from, and nothing after it is in the timeline: replaying the timeline
and then streaming draws each event exactly once. The streamed answer text gets
the same treatment, which also closes an older window in which a page opened
between an append and its publish drew one chunk twice.

### The page replays the timeline through the same handler

`adoptState`, joining a running turn, feeds the timeline to `handleEvent` — the
function that renders live frames — so a reloaded card is drawn by the same
code as a card watched from the start. Toasts are suppressed during the replay:
they announce what is happening now, and re-announcing what already happened is
noise.

### `round_usage`: usage per round, as it happens

`BaseAgent` emits `round_usage` after every completion with the stage, the
round, that round's `prompt_tokens` and `completion_tokens`, and the budget's
own `estimated_input_tokens` and `context_limit`. The page adds the running
turn's spend to the session total in the status bar and fills the context meter
from the round's prompt size. The estimate stands in when a runtime reports no
usage at all, so the meter still moves.

It is a `logic/` event, not a dashboard one: the terminal and the JSON adapter
receive it too.

## Consequences

### Positive

- A reload, a second tab, or switching to a session mid-run shows the stages,
  the activity and the streamed text the run has produced.
- The token count and context meter move while a stage runs.
- The five events the page did not handle (`turn_started`, `turn_finished`,
  `workflow_finished`, `tier_changed`, `session_resumed`) are now handled or
  explicitly ignored with a reason.

### Negative

- The JSON adapter's output gains one line per round.
- The bridge holds up to 400 events and two 6,000-character tails per running
  session: tens of kilobytes, in memory, for at most six sessions.
- A very long turn's oldest activity lines (beyond 400 events) are not
  redrawn after a reload; the live tab that watched them still has them.

### Mitigations

- `round_usage` is small and flat; consumers that do not know it ignore it, as
  every adapter already does with unknown event types.

## Alternatives considered

### Replay the bridge's frame buffer from the turn's first sequence

Rejected. The buffer is sized for reconnects, and a turn's own deltas evict its
beginning. Growing the buffer to hold a whole turn means holding thousands of
deltas per session to recover a few dozen events.

### Persist the timeline with the session

Rejected for now. A finished turn already lands in history with its receipt;
the timeline only matters while the turn runs, and a dashboard restart ends the
run anyway.

### Put usage in `tool_completed` or `stage_output_delta`

Rejected. A round without a tool call has no `tool_completed`, and a
non-streaming round has no delta. Usage is a property of the round, so it gets
its own event.
