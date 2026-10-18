# DR-044: Approvals Are Answered Inline, Listed Under "Needs You", and Can Be Granted for the Session

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `_Live.allowed_tools`, `resolve_approval(scope=…)` and `revoke_allowance` in `src/gui/bridge.py`; `POST /api/approval` (`scope`) and `POST /api/allowances` in `src/gui/server.py`; the approval cards, the "Needs you" section and the allowance chips in `src/gui/static/{index.html,app.css,app.js}`. Refines DR-012's dashboard surface; the gate itself is unchanged.

## Context

DR-012 puts every shell and network call in front of the person with its real
arguments. The dashboard did that with a full-screen overlay for the focused
session. Three problems showed up in use:

- **It seized the screen.** While it was up, nothing else on the page could be
  read or reached, including the run that was asking.
- **Waiting calls in other sessions were easy to miss.** They appeared as a
  toast and a dot on a rail row (DR-031), not as a list of what needs answering.
- **Only "once" or "refuse".** The terminal adapter already offers "allow this
  tool for the rest of the session"; in the dashboard a run that needed ten test
  commands needed ten clicks, the kind of prompt people learn to click through
  without reading — the approval-fatigue DR-024 warns about.

Kiro Crew answers approvals inline, lists what waits, and allows approving or
rejecting several at once.

## Decision

- **Inline cards.** A waiting call is a card in its session's conversation,
  under the run that asked, with the question in plain words ("Run this
  command?"), the exact arguments, an optional reason that goes back to the
  model on a refusal, and three answers: **Refuse**, **Allow `<tool>` for this
  session**, **Run it once**. With several waiting in one session, **Run all
  once** and **Refuse all** appear. Cards survive repaints, so a half-typed
  reason is not lost.
- **"Needs you".** The rail lists every waiting call from every session, and
  every held queue (DR-042), each opening its session. The tab title count
  (DR-043) covers the same set.
- **Allow for the session.** Answering with `scope: "session"` approves the call
  and records the tool on that session; later calls of that tool in that session
  are allowed without asking. The executor still records the request and the
  answer as events, so an allowed call is never invisible. Allowances are per
  session and per tool, never global, are listed in the rail with a way to take
  each back (`POST /api/allowances`), and end when the session is closed. A
  run the person asked to stop still refuses every call (DR-030).
- The approval overlay is removed.

## Consequences

### Positive

- The page stays usable while a call waits, and every waiting call is in one
  list.
- A run that needs the same command many times needs one decision.

### Negative

- An allowance covers every argument of that tool for the rest of the session:
  "allow `run_command`" allows any command. That is what the terminal already
  offered, and the reason it is per session and shown in the rail.
- Allowances are not stored with the session: a resumed session asks again.

### Mitigations

- The rail always shows what runs without asking, with a one-click revoke.

## Alternatives considered

### Allow by command pattern instead of by tool

Deferred. "Allow `pytest *`" is safer than "allow `run_command`", but it needs a
pattern language and a way to show what a pattern covers; this record keeps
parity with the terminal first.

### Keep the overlay and add the session option to it

Rejected. The overlay's problem was taking the screen, not missing a button.
