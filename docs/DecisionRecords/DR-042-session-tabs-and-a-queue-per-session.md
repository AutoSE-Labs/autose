# DR-042: Open Sessions Are Tabs, and Each Session Queues Prompts Sent While It Works

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** the per-session queue in `src/gui/bridge.py` (`submit(queue=…)`, `queue_action`, the `queue` frame and snapshot fields) and `POST /api/queue` in `src/gui/server.py`; session tabs, the queue strip and the composer in `src/gui/static/{index.html,app.css,app.js}`. Builds on DR-030 (the user holds the stop) and DR-031 (several sessions, one turn each). **Amended by [DR-056](DR-056-a-session-s-queue-is-saved-with-it.md): the queue below is now saved with the session on every change and comes back held when the session is reopened, so it survives a restart and closing the session.**

## Context

DR-031 lets several sessions run at once but keeps one turn per session, and
refuses a second prompt into a session that is working. That is the right rule
for the `Session` object, and an unhelpful one for the person at the keyboard:
the next instruction is usually already in mind while the current one runs, and
on a local model a run is minutes long. The options were to wait and remember,
or to open another session that does not share the conversation. Kiro Crew
answers this with a queue: send the next message now, it runs when the current
one finishes, and it can be edited, reordered or cancelled meanwhile.

The open sessions were also only visible in the rail, away from the
conversation.

## Decision

### A queue per session, held by the bridge

`POST /api/prompt` with `queue: true` into a session that is working appends the
prompt to that session's queue instead of being refused; without the flag the
answer is still a 409, now saying a prompt can be queued. When a turn ends, the
bridge starts the next queued prompt itself, so the queue advances with no
browser open. `POST /api/queue` removes or moves an item, clears the queue, or
resumes it. A `queue` frame announces every change, and the snapshot carries the
focused session's queue; the rail's summaries carry only a count.

The queue lives in the bridge, not in `logic/`: one `Session` still runs one
turn at a time, and the queue only decides when the next one starts.

### A stop holds the queue

After a turn the user stopped, or one that failed, the queue is **held**, not
advanced: a stop means "wait" (DR-030), and running the next request after a
failure would compound it. The held queue is shown with a Resume button. If the
next prompt is refused when it is due to start, it goes back to the front and
the queue is held.

### Tabs, and a composer that queues

The open sessions appear as tabs above the conversation, each with its status
(working, waiting for a decision, idle), its queue count and a close button —
the rail's actions, where the eye already is. While a run works, the composer's
button reads **Queue** beside **Stop**; Enter queues. DR-030's rule that Stop
never stands beside a dead, disabled button still holds, because the button
beside it now does something.

## Consequences

### Positive

- The next instruction can be given the moment it is thought of, and runs
  without anyone watching.
- Stopping or a failure never sets off the next request on its own.
- The open sessions are visible and switchable next to the conversation.

### Negative

- The queue is in memory: it does not survive a dashboard restart, though the
  sessions themselves do (DR-026). **Amended by DR-056:** it is now saved with
  the session.
- A queued prompt is run with the tier it was queued with, chosen before the
  previous turn's outcome was known.

### Mitigations

- The queue is shown in the conversation, so what will run next is never a
  surprise, and each item can be removed or reordered.

## Alternatives considered

### Keep the queue in the browser

Rejected. A queue in one tab stops advancing when the tab is closed and is
invisible to another tab on the same dashboard; the bridge already owns when a
turn starts.

### Steer: deliver the message into the running turn

Deferred. Injecting text into a turn in flight needs the agent loop to take
input between rounds and every stage to know what to do with it; that is a
`logic/` change with its own record.

### Run the queue after a stop as well

Rejected. A stop is the user saying the run went wrong or should pause;
starting the next request then would override that.
