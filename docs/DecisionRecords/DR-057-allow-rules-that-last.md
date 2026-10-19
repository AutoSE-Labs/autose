# DR-057: Allow Rules That Last: Saved With the Session, and for a Workspace

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/saved_allowances.py` (`AllowanceFile`, `recheck_rule`); `SessionRecord.allowances` and `allowances_seal` in `src/logic/persistence.py`; `Session.allowances`, `dropped_allowances`, `keep_allowances`, `snapshot` and `restore` in `src/logic/session.py`; the bridge's `_Live.allowances`, `_ask`, `resolve_approval`, `add_allowance`, `revoke_allowance` and the state payload (`workspace_allowances`, `dropped_allowances`) in `src/gui/bridge.py`; `POST /api/allowances` (`scope`); the Allowed page in `app.js` and `index.html`; `TerminalApprovalGate` (`workspace_rules`, `adopt`) and `/tools` in `src/tui`. Refines DR-044, DR-048, DR-050 and DR-052; DR-047's rule for unattended runs is unchanged.

## Context

"Always allow" (DR-048) and rules written by hand (DR-050) were held in
memory for one session. Reopening a session asked again about `pytest`, and
there was no way to say "in this project, `pytest` never needs asking". The
obvious place for both is the workspace's `.autose/` directory, but anything
in the workspace can arrive with a clone or a pull: a repository must never be
able to grant itself permission to run commands.

## Decision

- **A session's own rules are saved with it.** `SessionRecord` gains an
  optional `allowances` list of `(tool, pattern)` pairs (a session saved
  before this has none). Every change, from a question card, the Allowed page
  or the terminal's `a`, is saved at once when the session is already on disk,
  else with its first turn.
- **Saved session rules carry a seal.** The record lives in the workspace, so
  its rules are sealed with an HMAC over the workspace path, the session id
  and the rules, made with a key kept in the user's config directory. A
  session whose rules do not match their seal (one that came in a clone, was
  edited by hand, or was saved on another computer) reopens with none of
  them, and says so.
- **Workspace rules** apply to every session in a workspace. They live in
  `<user config dir>/allowances.json`, keyed by the workspace's resolved path,
  never in the workspace, as the watcher commands this computer trusts do
  (DR-047). Every read goes back to the file, so a rule added in the
  dashboard reaches a terminal open on the same workspace at its next
  question.
- **Everything read back is checked again.** Each stored rule, from either
  place, goes through `allowance_for_rule` (and so `offer_for`) as a rule
  typed by hand would, and must come back exactly as written. A rule today's
  rules would refuse (`rm`, a wrapper, a tidied URL) is dropped with its
  reason, not trusted.
- **Coverage.** A call runs without asking when `standing_for(request,
  session rules + workspace rules)` covers it: in the dashboard's `_ask`, for
  calls already waiting when a rule is added, and in the terminal gate. A
  chain may be covered by parts from both.
- **Unattended runs are unchanged.** Scheduled and watched runs (DR-047) do
  not use workspace rules: their gated calls are still refused unless the
  dashboard was started with `--yes`. A rule given for the person's own
  sessions is not consent for runs nobody is watching.
- **Dashboard.** `POST /api/allowances` takes `scope` (`"session"`, the
  default, or `"workspace"`) for add and revoke. Adding a session's own rule
  for the workspace moves it there ("Keep for this workspace"). The Allowed
  page shows "This session" and "Every session in this workspace"; the add
  form asks which; the question card keeps "Always allow … here" for the
  session; the session bar's count covers both.
- The terminal has no command to add a workspace rule; it honours them and
  lists them in `/tools`.

## Consequences

### Positive

- Reopening a session no longer asks again about what was already allowed,
  and a project's routine commands can be allowed once for every session.
- Nothing a repository contains can add a rule: workspace rules are outside
  it, and session rules in it only count when this computer sealed them.
- A rule saved under looser rules cannot outlive a tightening of them.

### Negative

- Losing or replacing `allowances.json` loses the workspace's rules and the
  sealing key, so every saved session asks again. That is the safe direction,
  but it is a surprise.
- A workspace that moves on disk is a different workspace: its rules and its
  sessions' seals stay with the old path.
- A rule whose words cannot be written back the same way (a script name with
  a space in it) is dropped on reopening, as it could not be typed by hand.
