# DR-071: Validate Gets the Run's Diff

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/workflows/run_changes.py` and the validate stage's input in `src/logic/workflows/staged.py`. Applies to `craft` and `forge`. DR-004's clean memory per stage and DR-008's read-only Validate are unchanged. Validate's instructions are DR-072.

## Context

Each stage starts with a clean memory (DR-004). Validate gets the request, the plan and Execute's own report of what it did, but not the change itself.

On qwen3:8b, with a small `craft` request (make `divide()` raise `ValueError` on zero, and add a test), Validate took 72 of 184 seconds in the fastest run measured. About 35 of those were three turns listing the workspace and re-reading `calc.py` and `test_calc.py`, which Plan and Execute had already read. In another run, Validate approved a test that had never been written, writing "confirmed the test case exists and passes", on Execute's word that it had. The change was not in front of it, so nothing contradicted that.

## Decision

**Every Validate gets the run's diff.** The run's change journal (DR-024) holds each file as it was before the run. AutoSE diffs that against the file on disk now and adds the result to Validate's input under "What changed in this run", saying it was computed by AutoSE and is not the implementation stage's account. Reading the disk, not the journal's last write, means a file a command changed afterwards is shown as it now is. A file put back as it was is not shown. At most 12,000 characters of diff are included; a file whose diff would go over is named, and Validate reads it itself. Files changed only by a shell command are not journalled, so they are not shown, and the section says so. Without a journal, Validate's input is as before.

## Consequences

### Positive

- Validate no longer needs turns to find out what changed. Results of the real runs are in the commit message.
- Validate can see a missing test, a deleted test or a test that never imports what it uses, without trusting Execute's report.

### Negative

- Validate's input is longer by the size of the diff, up to 12,000 characters.

## Alternatives considered

- **Use `git diff`.** It shows changes made before the run as well, and nothing in a workspace that is not a git repository. The journal holds exactly what this run changed.
- **Hand Validate the full changed files instead of a diff.** A diff shows what was removed, such as a deleted test, which a full file does not. Validate can still read the full file.
- **Skip Validate for small changes.** In the one run where Validate caught something, the change was small: Execute had never added the requested test.
