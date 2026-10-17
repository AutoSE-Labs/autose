# DR-041: The Inspector's Usage Tab Shows Where the Tokens and the Time Went

- **Status:** Accepted
- **Date:** 2026-10-17
- **Decision owners:** AutoSE maintainers
- **Scope:** the inspector's Usage tab (`src/gui/static/inspector.js`, `app.js`, `app.css`); the status bar's context meter. Uses DR-033's `round_usage` event and the stage events' timestamps; no `logic/` change.

## Context

The status bar shows a session's token total and how full the context window
was. Neither answers the questions that come up when a run is slow or
expensive: which stage spent the tokens, how many rounds it took, how long it
ran, and whether the window was close to full at any point. Reviewers of Kiro
Crew name the lack of exactly this breakdown as its main gap. AutoSE already
emits the facts — `round_usage` per model call (DR-033), stage start and finish
events with timestamps — and nothing showed them together.

Building the panel also exposed a bug in the meter itself: when a turn
finished, the meter was set from the turn's `prompt_tokens`, which is the sum
over every round. A run that never used more than 5% of its window showed 67%.

## Decision

- **Usage tab.** Three tiles for the session (total, prompt, completion); a
  table for the run this page watched, one row per stage with its rounds,
  prompt and completion tokens and duration; a bar per round showing how much
  of the window that request filled, with the peak stated; and a bar per turn of
  the session showing its share of the tokens. A stored turn contributes its
  totals; the per-round and per-stage detail exists only for runs the page saw.
- **The meter shows the last round.** At the end of a turn the context meter
  keeps the last round's prompt size (or the budget's estimate where the runtime
  reports none); the turn's summed `prompt_tokens` is only a fallback when no
  round was seen.
- **Charts are SVG with sizes as attributes.** The dashboard's
  Content-Security-Policy is `default-src 'self'` (DR-029), which silently drops
  inline `style` attributes; the first version of these bars drew nothing for
  that reason. Sizes go in SVG attributes, colours in classes, and a test fails
  if a page script writes an inline style.

## Consequences

### Positive

- Where a run's tokens and time went is visible per stage and per round.
- The context meter reports the quantity it is labelled with.
- The CSP stays as strict as it was.

### Negative

- Per-round and per-stage detail is not stored with the turn, so a page opened
  after a run shows only that turn's total.

### Mitigations

- The events are replayed to a page opened mid-run (DR-033), so the detail is
  complete for any run in progress.

## Alternatives considered

### Relax the CSP to allow inline styles

Rejected. `style-src 'unsafe-inline'` widens what injected markup could do, to
save drawing a bar a different way.

### A charting library

Rejected. The page loads nothing from the network and has no build step
(DR-029); a few rectangles do not justify either.
