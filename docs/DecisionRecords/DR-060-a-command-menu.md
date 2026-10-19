# DR-060: A Command Menu in the Dashboard

- **Status:** Accepted
- **Date:** 2026-10-20
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/gui/static/palette.js`, its window in `index.html`, its
  styles in `app.css`, `/palette.js` in the server's `_ASSETS`, and the
  `palette` entry in `overlays` and `closeOverlays()` in `app.js`.

## Context

The dashboard does a great deal from many places: the header, the rail, the
session bar, the composer, the details panel and half a dozen windows. Doing
any of it means knowing where its control lives. People asked for a single
place to reach everything by name from the keyboard, as editors offer.

## Decision

- **Ctrl K** (**Cmd K** on a Mac, where Control-K deletes to the end of the
  line in every text field) or the search button in the header opens a menu:
  a search box over the dimmed page and the commands under small headings
  (Waiting for you, This run, Sessions, Go to, Settings, Files).
- **It has no behaviour of its own.** Each command calls what the page's own
  control calls (`decide`, `openDiff`, `setYolo`, …) or presses the control.
  What a control asks to confirm is still asked (undo, clear, YOLO under
  DR-053); what it refuses during a run is shown as unavailable with the
  reason. Nothing new runs without asking (DR-012, DR-048).
- **Answering what waits is held to what can be read.** "Run it" and
  "Always allow" are offered only for a one-line call of at most 160
  characters, shown whole on a row that wraps and keeps its spaces. A longer
  call, or one on several lines (each line a command of its own), is offered
  as "Read it in full", which opens its card or Needs you and answers
  nothing; "Don't run" is always offered. A plan is approved from the menu
  only when it is the one on screen (DR-045).
- **Enter by reflex answers nothing.** Before anything is typed, the row
  Enter runs is never one that answers a call, approves a plan, resumes a
  held queue or stops a run; those rows are still listed first, and are
  chosen by moving to them or by typing.
- Matching is forgiving: a substring, the first letters of the words, or the
  letters in order with a few gaps. A session answers to its number; files
  come from the Files view's index (DR-051).
- The five commands used last come first next time, kept in this browser's
  `localStorage`. One-off answers are never remembered.

## Consequences

### Positive

- Every control is reachable from the keyboard without knowing where it is.
- Calls from every session can be answered from one place, under the same
  rules as their cards.

### Negative

- A second place answers approvals; it has to keep following the card's
  rules as those change. The tests run `palette.js` in node against
  stand-ins for the page, so what it offers is checked as behaviour.
- The commands are read when the menu opens and on each key typed, not
  while it sits open; a call that arrives meanwhile shows after the next key.

## Alternatives considered

- **Approving any call from its summary.** Rejected: a row is cut short, and
  a command cut in the middle can hide what it does (`pytest … | sh`).
- **Ctrl K on every platform.** Rejected: on a Mac it takes the text
  fields' own Control-K.
