# DR-063: Suggested Follow-ups Are Worked Out From the Run, Not Generated

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/followups.py` (`suggest_follow_ups`, `FollowUp`); `turn_payload` (`src/gui/payloads.py`); the follow-up chips above the request box (`index.html`, `app.js`, `app.css`); `follow_ups_line` (`src/tui/render.py`) and `App._render_outcome` (`src/tui/app.py`). Reads DR-004, DR-024, DR-030 and DR-038; changes none of them.

## Context

When a run ends, the next request is often already decided by how it ended:
a requirement failed its check, the person pressed Stop halfway, a file was
changed with no test beside it, an answer offered a fix without making it. The
person still had to work that out and type it, and the request box gave no
hint.

A model could write the suggestions, but on the hardware AutoSE targets
(DR-015) that is a second generation after every run. It holds the GPU the
person is about to need, arrives seconds after they have started typing, and
can differ between the dashboard and the terminal.

## Decision

- `suggest_follow_ups(turn)` in logic is a pure function of a finished turn.
  It reads only fields the stored record also has (`prompt`, `answer`,
  `stop_reason`, `requirements`, `route`, `changes`, `undone`), so a turn
  reloaded from disk gets the suggestions it had live. No model is called.
- The rules, in order of how much they matter:
  - A run stopped by the person or by a limit (`cancelled`, `round_cap`,
    `attempts_exhausted`, `time_budget_exhausted`): "Carry on from where it
    stopped". Any other failure: "Try again: <what failed>", in plain words.
  - Requirements the validator marked Failed, and, in a run that completed,
    requirements it never verified (DR-038): "Finish R3: <requirement>", at
    most two of them. In a run that stopped, an unverified requirement is
    left to "carry on".
  - Files still changed (DR-024; undone ones do not count): "Add tests for
    <file>" when the run completed, changed a source file and touched no test
    file; "Run the tests" when it completed and changed code or tests;
    "Explain what you changed in <file>" whatever the ending.
  - An answer that changed nothing: "Make this change" when it offers a
    change and names some code; otherwise a deeper question only when the
    answer names a file or a function the request did not ("Walk me through
    classifier.py", "Show where ratchet is used"). Nothing at all is better
    than a vague suggestion.
- Never more than three, no repeats, no empty label or request, and no
  decision-record number. A chip shows a short label; the request behind it is
  complete on its own, because an unfinished run never reaches the model's
  memory (DR-004), so "carry on" must restate what to carry on with.
- The turn payload sends them as `follow_ups: [{label, prompt}]`, worked out
  each time and never stored.
- The dashboard shows them as pill chips above the request box, for the
  focused session's latest finished run only. A chip puts its request in the
  box and never sends. They hide while the box has text, while a run is going,
  after a run that failed without a turn, and for that run once the × is
  pressed.
- The terminal prints them as one dim "next:" line after the result.

## Consequences

### Positive

- Instant and free: no GPU time, no wait, nothing to load.
- The dashboard and the terminal always agree, and a reloaded session shows
  what it showed live.
- Each rule is a small function with its own tests, so wording and thresholds
  can be tuned without touching the adapters.

### Negative

- "Make this change" rests on phrasing ("I suggest", "you could change …")
  in an answer that names code. An unusual phrasing is missed, and advice
  that happens to name code can be taken for a change.
- The rules cannot see what a run meant, only how it ended and what it
  touched; a good next question that needs understanding the answer is not
  offered.
- Test files are recognised by their usual names and folders. A project with
  another convention gets "Add tests for" when it already has some.
