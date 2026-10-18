# DR-047: Schedules, Watchers, and the Dashboard as a Long-Running Local Service

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** new `src/logic/automation.py` (cadences, cron parser, watcher triggers, command trust, the store); new `src/gui/automation.py` (the ticking runner); new `src/gui/service.py` (stable token, systemd unit writer); unattended sessions, `automation()` / `automation_action()` and the `automation` snapshot summary in `src/gui/bridge.py`; `GET`/`POST /api/automation` in `src/gui/server.py`; `--service` and `--install-service` in `src/gui/cli.py` and `src/tui/cli.py`; `refuse_escaping_command` made public in `src/logic/common/tools/shell.py`. Builds on DR-012 (approvals), DR-026 (durable sessions), DR-029 (local dashboard and its trust model), DR-030 (the user holds the stop), DR-031 (concurrent sessions), DR-036 (the user config directory), DR-042 (the bridge owns per-session scheduling of turns) and DR-043 (notifications).

## Context

Everything AutoSE did until now started with a person typing. A lot of useful
work does not: a morning digest of what changed overnight, a weekly dependency
check, "tell me — and start fixing it — when CI goes red". Kiro's Crew calls
this work that runs while you are away: recurring prompts, and heartbeats that
watch something and act when it changes.

DR-031 and DR-042 already made the dashboard able to run several sessions and
start a turn with nobody clicking (a queue advances by itself). What was
missing: something that decides *when* to start a turn, a definition of what
is due, a rule for approvals when nobody is there, and a dashboard that keeps
running — and keeps its address — across restarts.

## Decision

### Schedules

A schedule is `{id, name, prompt, tier, every, enabled, created_at,
last_run_at, last_session, next_run_at, last_outcome}`. `every` is either a
whole number of minutes (1 or more) or a five-field cron expression — minute,
hour, day of month, month, day of week — with numbers, `*`, `*/n`, `a-b`,
`a-b/n` and comma lists, matched in **local time**. As in cron, when both day
fields are restricted, a day matching either is due. An expression that is
well formed but never fires (`0 0 31 2 *`) is refused when saved. `tier` empty
means the session's usual classification.

The next run counts from the last run (or from creation, or from being
re-enabled). Runs missed while nothing was ticking collapse into one: a laptop
asleep all night runs an hourly schedule once on waking, not eight times.

### Watchers

A watcher is `{id, name, command, every_minutes, trigger, prompt_template,
tier, enabled, last_checked_at, last_output_digest, last_exit_code,
last_triggered_at, last_session, last_outcome}` plus an internal
`pending_prompt`. Every `every_minutes` its command runs in the workspace root
with a 60-second timeout; its combined output is hashed. The **first check only
records a baseline**. After that the trigger decides:

- `change` — the output differs from the previous check's;
- `failure` — the command failed *anew*: a non-zero exit after a success, or a
  failure whose output changed. A failure that persists unchanged does not
  start a run every interval.

When it fires, `prompt_template` becomes the prompt, with `{exit_code}` and
`{output}` (the last 4,000 characters) substituted by plain replacement, so
braces in output or template need no escaping. A triggered prompt that cannot
start yet is kept as `pending_prompt` and retried next tick without re-running
the check, so a full desk does not lose an event.

Example — a CI watcher:

```json
{
  "action": "add_watcher",
  "name": "CI",
  "command": "gh run list --limit 1 --json status,conclusion",
  "every_minutes": 5,
  "trigger": "change",
  "prompt_template": "The latest CI run is now:\n{output}\nIf it failed, find out why and propose a fix."
}
```

### The trust decision for watcher commands

A watcher's command does **not** go through DR-012's per-call approval gate.
The person typed it into the dashboard's settings; that is the same trust as a
custom tool defined in a profile or a command in a config file they wrote, and
an approval prompt every five minutes for a command they authored would be
answered without reading — the failure DR-012 exists to avoid. What still
applies:

- the same workspace-confinement checks as `run_command`
  (`refuse_escaping_command`: no absolute, home or parent paths, no `cd`) at
  save time and again at run time — with the same caveat that they are
  pattern checks, not a sandbox;
- a timeout that cannot be turned off, and bounded output into the prompt;
- **authorship is checked, not assumed.** Definitions live in
  `.autose/automation.json` in the workspace, and a file in a repository was
  not necessarily written by the person running the dashboard — it can arrive
  in a clone or a pull. A command runs only if its digest was recorded when it
  was saved through the dashboard on this machine; that record is kept in the
  user config directory (`trusted-watchers.json`, per workspace, 0600),
  outside any repository. An unrecognised command is reported as refused with
  instructions to open and save it.

The *session* a watcher or schedule starts is an ordinary agent run, and every
tool call in it is gated as usual — see the next rule.

### Unattended runs never wait for approval

A session opened by a schedule or watcher is **unattended**. An
approval-required call in it is denied immediately, with the reason
"unattended run: `<tool>` needs a person; run it from the dashboard to approve
it", instead of being parked for the usual fifteen minutes. The only exception
is a dashboard started with `--yes`, the existing attributed opt-in, which
never consults the gate at all; `GET /api/automation` reports which posture is
in force as `unattended_approvals: "deny" | "auto"`. A prompt a person sends
into such a session afterwards is attended, and its approvals reach the
browser as usual. This mirrors Kiro: scheduled runs are unattended by
definition.

### What runs where

- `logic/automation.py` is pure: parsing, validation, next-run computation
  from an injected `now`, the trigger and trust strategies, `due(now)`, and the
  store with atomic writes. No threads, no clock — unit-testable with a fake
  clock and a fake command runner. An unreadable file is reported in
  `problems` and kept as `automation.json.unreadable` rather than overwritten.
- `gui/automation.py` is the runner: a daemon thread that ticks every 30 s
  (injectable), runs due schedules, retries pending watchers and checks due
  ones, and publishes an `automation` frame when something ran, was skipped,
  or a check was news.
- `gui/bridge.py` opens an unattended session per run, titled with the
  schedule's or watcher's name, without taking the focus. If the desk is at
  `MAX_OPEN_SESSIONS` (DR-031), finished unattended sessions that are not
  focused are closed first (they are on disk, DR-026); a session the person
  opened is never closed for this. If there is still no room the run is
  skipped with `skipped: desk full …` and stays due. A schedule whose previous
  run is still working is skipped rather than started beside itself.

### The gateway: service mode and a unit file

- `--service` (on `autose`, implying `--web`, and on `python -m gui`) runs the
  dashboard as a long-lived local service: no browser is opened, and the token
  is read from `dashboard.token` in the user config directory, created once
  with 0600 (an existing looser file is tightened). Bookmarks and notification
  links survive restarts; deleting the file rotates the token. The host,
  `Host`/`Origin` checks and token requirement of DR-029 are unchanged.
- `--install-service [--workspace DIR] [--port N]` *writes*
  `~/.config/systemd/user/autose-dashboard.service` (honouring
  `$XDG_CONFIG_HOME`) running `python -m gui --service` for that workspace,
  and prints `systemctl --user enable --now autose-dashboard`. It never runs
  `systemctl`: starting a background agent is the person's decision. On
  anything but Linux it says it is not supported there.

### API

- `GET /api/automation` → `{schedules, watchers, problems, running,
  unattended_approvals, authorized_by, file}`.
- `POST /api/automation` with `action` one of `add_schedule`,
  `update_schedule`, `delete_schedule`, `run_now`, `add_watcher`,
  `update_watcher`, `delete_watcher`, `check_now`, plus the fields. A
  validation failure is a 400 with the reason, an unknown id a 404, a
  `run_now` that cannot start (desk full) a 409.
- `/api/state` carries `automation: {schedules, watchers, next_run_at,
  next_check_at, running}`.

## Consequences

### Positive

- Recurring and event-driven work runs with no browser open, and every run is
  an ordinary stored session the person can open, read and undo.
- Nothing new can execute unreviewed: the commands that run without a prompt
  are only the ones the person typed on this machine, and the agent's own
  calls in unattended runs are refused rather than approved.
- Scheduling logic is testable without a thread or a real clock.

### Negative

- An unattended run cannot use `run_command`, `web_search` or `fetch_url`
  unless the whole dashboard runs with `--yes`. Many useful scheduled prompts
  (run the tests and report) are therefore read-and-write-only, or need a
  person to follow up.
- Automation only runs while the dashboard runs. With no service installed,
  closing the terminal stops it.
- Cron is matched in local time, and daylight-saving changes are handled on
  a best-effort basis: a local time skipped in spring runs at the next valid
  instant, and a schedule inside the hour repeated in autumn may run once or
  twice.
- The ticking runner checks watchers sequentially; a slow probe (up to its
  60 s timeout) delays the rest of that tick.
- The service's token is printed at start-up, so it appears in the user's
  journal (readable by that user only).

### Mitigations

- The denial reason names the tool and says to run it from the dashboard; a
  prompt sent there afterwards is attended. `--yes` remains available, and
  attributed, for those who want unattended approval.
- `--install-service` exists so "runs while you are away" does not depend on
  a terminal staying open.

## Alternatives considered

### OS cron or systemd timers driving `autose --json`

Kept as a documented alternative, not built in. `autose --json "<prompt>"`
(DR-027) already runs one prompt headlessly and exits non-zero on failure, so
a crontab line or a systemd timer works today for anyone who prefers it. It
was not chosen as *the* mechanism because the results would not appear in the
dashboard as they happen, each run pays start-up and model discovery again,
watchers would need their own state files, and a cross-platform story would
mean three schedulers instead of one.

### A third-party scheduling library (APScheduler, croniter, `schedule`)

Rejected. AutoSE's dependency list is deliberately two packages and the
dashboard is standard library only (DR-029). The subset of cron needed here is
a hundred lines that are tested in full.

### Letting unattended runs wait for an approval

Rejected. With nobody watching, every gated call would hold its run for the
full approval timeout and then be denied anyway; the session would sit "busy"
for fifteen minutes per call and block the next scheduled run. Denying at once
gives the same outcome without the hostage, and says how to get the call made.

### Putting the definitions in the user config directory instead of the workspace

Rejected for the definitions — schedules are about a workspace and belong
with it, next to its sessions — but adopted for the *trust record*, which is
exactly the part a repository must not be able to supply for itself.

### Server-side desktop notifications

Still left to the page (DR-043): the `automation` frame lets an open
dashboard notify; a server-side notifier remains a possible follow-up for a
service with no page open.
