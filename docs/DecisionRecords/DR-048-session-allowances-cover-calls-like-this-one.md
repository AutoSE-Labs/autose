# DR-048: "Allow for This Session" Covers Calls Like This One, Not the Whole Tool

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/common/allowances.py` (`Allowance`, `AllowanceOffer`, `offer_for`); the bridge's `_Live.allowances`, `resolve_approval`, `revoke_allowance` and the approval payload in `src/gui/bridge.py`; `POST /api/allowances` (`id`); the approval card and allowance chips in `src/gui/static/app.js`; `TerminalApprovalGate` in `src/tui/approval.py`. Refines DR-044 and the terminal's "always allow"; DR-012's gate is unchanged.

## Context

DR-044 let the dashboard answer an approval with "allow for this session", as
the terminal's `a` already did, and recorded the weakness: the allowance was per
tool. Saying yes to `pytest` for the session said yes to `run_command`, which
runs any shell command — `rm -rf`, `curl … | sh`, `git push`. The person reading
the card sees one command and approves a category they did not see. DR-044
deferred "allow by command pattern"; this record makes that decision.

## Decision

One rule, in `logic/`, read by every adapter: `offer_for(request)` returns what
a standing yes for this call would cover, or why it can only be answered once.

- **Commands** are covered by their leading words: the program, plus its
  subcommand for tools that have them (`git status`, `npm run test`), plus the
  script or module for interpreters (`python manage.py`, `python -m pytest`),
  recursively through runners (`uv run pytest`). A later command is covered only
  when the same derivation gives the same words, so `git status` does not cover
  `git push`, and every rule that refuses an offer also refuses a match.
- **Only once**, with the reason shown, for: shell syntax (`;`, `&&`, `|`,
  redirects, `$(…)`, backticks); leading environment assignments; wrappers that
  run an arbitrary command (`sudo`, `env`, `xargs`, `sh`, `bash`, `ssh`, …);
  inline code (`python -c`, `node -e`); options before a subcommand
  (`git -C elsewhere …`); container and remote execution (`docker run`,
  `kubectl exec`); and commands that delete, change permissions, stop
  processes, reach the network or publish (`rm`, `chmod`, `kill`, `curl`,
  `git push`, `git reset`, `npm publish`, …).
- **Page fetches** are covered by host: "pages on docs.python.org".
- **Other gated tools** (`web_search`, and the edit tools when a workspace
  gates them) are covered as a whole, as before.

In the dashboard the button reads "Allow `pytest …` for this session"; a call
with no offer shows "Only once:" and the reason instead of the button. Allowing
answers any waiting call the new allowance covers. Chips under "Needs you" name
what is allowed and revoke one allowance each. In the terminal, `a` on a
call with no offer runs it once and says why the next one will still ask.

## Consequences

### Positive

- The scope of a standing yes is what the card shows, not a category behind it.
- Common loops — running the tests, checking `git status`, reading one docs
  site — still need one decision.

### Negative

- The rules are word checks, not a sandbox. `pytest` with a hostile
  `conftest.py`, or a project script that deletes files, is still allowed once
  its leading words are.
- Some safe commands get no offer (anything piped), and some lists (wrappers,
  destructive commands) will need additions as they are met.

### Mitigations

- Every allowed call is still recorded as an approval event, and the chips show
  what runs unasked, with one-click revoke.
- The lists live in one module with a test per rule.

## Alternatives considered

### User-written glob patterns ("allow `pytest *`")

Rejected for now. A pattern box needs its own language and a preview of what it
matches, and the common case is "this command, again", which the derivation
gives without typing.

### Keep per-tool allowances and warn on the button

Rejected. A warning on a button people click to stop being asked is read once.
