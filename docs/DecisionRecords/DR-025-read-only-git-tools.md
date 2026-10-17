# DR-025: Read-Only Git Inspection Is a Built-In Tool, Not an Approved Shell Command

- **Status:** Accepted
- **Date:** 2026-08-30
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/tools/git.py`, the tool catalog, every stage's read-only registry. **Refines DR-008 and DR-012.**

## Context

Every stage prompt tells the model to prefer the file tools over
`run_command`, and then names git as one of the things `run_command` is *for*
— `spark`'s prompt said "reserve it for things files cannot tell you — `git`
state, a test run, an installed version."

That made the single most common piece of context an agent needs — what is
uncommitted, what changed, what happened recently — reachable only through the
one tool that stops and interrupts the user. Three costs followed:

- Every `git status` cost a human interruption, against a gate that exists for
  arbitrary shell execution.
- Users approving `git status` twenty times get faster at approving things,
  which is the approval-fatigue problem DR-012 exists to avoid.
- The model had to *compose a shell command*, so a small local model's
  malformed `git log --pretty=…` became a failed round rather than a
  well-formed call against a schema.

DR-012 gates `run_command` because it runs *arbitrary* shell. That reasoning
does not transfer to a fixed, read-only subcommand any more than it transfers
to `read_file`.

## Decision

`git_status`, `git_diff`, and `git_log` are built-in tools in
`READ_ONLY_TOOL_NAMES`, available to every stage entitled to observe the
workspace, and **not** approval-gated.

Three properties make that safe, and each is enforced in code rather than
asserted in a docstring:

- **No shell.** Every invocation is an argv list passed to `subprocess.run`
  with `shell=False`. Nothing in a model-supplied value can become a command
  substitution, a pipe, or a second command.
- **No model-supplied flags.** The only free-text argument any handler takes
  is a path. It is resolved through `Workspace` — so it cannot name a file
  outside the root — and placed after a `--` separator, so `--upload-pack=…`
  reaches git as a pathspec that matches nothing rather than as an option.
- **No revisions.** None of these handlers accepts a revision or a range. A
  revision is where an option can hide, and the questions these tools exist to
  answer do not need one.

Anything past those three subcommands stays with `run_command`, where a human
reads the exact command first.

`_read_only()` in `workflows/stages.py` now returns `READ_ONLY_TOOL_NAMES`
rather than restating a list, so a read-only tool added to the catalog reaches
every entitled stage instead of whichever stage remembered to list it.

### A workspace with no git history is answered, not failed

All three handlers return an ordinary informational string — "Not a git
repository: this workspace has no git history to inspect. Answer from the
files themselves." A local-first tool is routinely pointed at a directory that
is not a repository, and a stage should move on rather than retry.

## Consequences

### Positive

- The most common repository question no longer interrupts the user, which
  makes the remaining approval prompts rarer and therefore more likely to be
  read.
- A schema replaces command composition, so a small model asking for recent
  history cannot get the invocation wrong.
- Output is shaped for a model: porcelain status codes are expanded into
  words, because ` M` and `M ` mean opposite things about the index and a
  small model reads that wrong.
- These compose with DR-024: an agent can inspect the repository state its own
  edits are landing against.

### Negative

- Three more tool schemas in every stage's registry, against a context window
  that may be 4k (DR-022). Roughly 250–300 tokens on every round of every
  stage. Judged worth it because each avoided `run_command` round costs far
  more than that — but it is a real, recurring cost, not a one-off.
- Three fixed subcommands is a narrower surface than git offers. `git show`,
  `git blame`, and branch comparison are all reasonable next asks and all
  require accepting a revision, which is the argument this decision declined
  to accept.

### Mitigations

- Environment hardening on every call: `GIT_OPTIONAL_LOCKS=0` so an inspection
  never blocks on or takes the index lock, `GIT_PAGER=cat` so a repository
  that configures a forced pager cannot hang a captured pipe, and
  `GIT_TERMINAL_PROMPT=0` so nothing can block waiting for credentials.
- A 30-second timeout, so a hung git does not hang the stage.

## Alternatives considered

### Keep git behind `run_command` and improve the prompt

Rejected. It leaves the interruption in place, leaves command composition to
the model, and leaves the approval prompt being answered reflexively — the
three actual problems.

### One `git` tool taking a subcommand string

Rejected. It reintroduces exactly what the argv list removes: the model
supplies a string that has to be parsed and validated into arguments, and the
validator becomes the security boundary. Three narrow tools have no such
boundary to get wrong.

### Accept a revision argument, validated against a pattern

Rejected for now. A revision is expressive enough that "does this look like a
revision" becomes a real parser, and git accepts option-like strings in
revision position in ways that are genuinely surprising. Declining revisions
costs some capability and removes the whole class of question.

### Gate the git tools like `run_command`

Rejected. The gate exists for arbitrary execution. Gating a fixed, read-only
subcommand adds interruptions without adding safety, and makes the gate mean
less by attaching it to something harmless.

### Implement the inspection with a git library instead of subprocess

Rejected. It adds a dependency to a project whose entire dependency list is
PyYAML and rich, to reimplement three commands that the installed git already
answers correctly and identically to what the user sees in their own terminal.
