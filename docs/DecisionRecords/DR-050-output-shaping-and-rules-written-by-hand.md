# DR-050: Output Shaping Does Not Block a Standing Yes, and Rules Can Be Written by Hand

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/allowances.py` (`_without_output_shaping`, `allowance_for_rule`); `Bridge.add_allowance` in `src/gui/bridge.py`; `POST /api/allowances` (`action: "add"`); the allowed-commands panel in `src/gui/static/app.js`. Refines DR-048.

## Context

DR-048 refused a standing yes to any command using shell syntax. In practice
the model appends `2>&1` or `| tail -5` to most commands it runs, so the
commonest calls (`pip install -r requirements.txt 2>&1 | tail -5`,
`pytest -q 2>&1`) could only ever be answered once, and the person was asked
the same question every time. Separately, what a session runs without asking
could be seen and taken back, but not added to before the first card.

## Decision

- **Output shaping is set aside before a command is judged.** A trailing pipe
  into a filter that only reads (`tail`, `head`, `grep`, `egrep`, `fgrep`,
  `wc`, `cat`) whose arguments hold no further shell syntax, and output
  redirected to `/dev/null` or stderr merged into stdout (`2>&1`), are removed;
  what remains goes through DR-048's rules unchanged. `sort` and `uniq` are not
  filters here, since both can write files; nor is `tee`. A pipe into anything
  else, a redirect into a file, `;`, `&&`, `$(…)` and backticks still mean
  "only once".
- **The match uses the same derivation**, so an allowance for `pip install`
  covers `pip install fastapi 2>&1 | tail -3`.
- **A rule can be written by hand** for a session: a command's leading words,
  a host, or a whole tool among those DR-048 already allows as a whole. A
  command rule must be exactly the words DR-048 would derive from it, so a
  hand-written rule never covers more than answering a card could
  (`pip install -r x` is refused with "write pip install"; `rm` is refused).
  Adding a rule answers any waiting call it covers.

## Consequences

### Positive

- The commands the model actually writes can be allowed once and then run.
- The person can set up what runs without asking before a run starts, in one
  place, and see and remove it there.

### Negative

- A filter's own work is trusted: `| grep x` reads its input and prints. A
  filter list that grows carelessly would widen what a click allows; additions
  must be read-only and take no output file.
- Allowances are still per session and are not saved with it.
