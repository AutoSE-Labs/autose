# DR-068: One Approving Validation Pass Ends the Run

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** `StagedWorkflow` in `src/logic/workflows/staged.py`, `src/logic/workflows/stages.py`. Applies to `craft` and `forge`.

## Context

Since 2026-10-18 (commit `03c318b`, ported from AutoSE Pro), a Validate pass that approved the work did not end the run. A second Validate pass started from scratch, without the first one's report, and was told to check the request sentence by sentence. Only if it also approved did the run end.

No decision record covered this. The only reason given was a code comment: on DeepSWE benchmark tasks with a local 27B model, one Validate pass approved work that broke a rule the request stated plainly, while the run still had time left. No saved run shows the second pass catching something the first one missed.

On qwen3:8b on an Apple M4, for a small `craft` request (make `divide()` raise `ValueError` on zero, and add a test), the second pass took 33–50 seconds, a fifth to a third of the run. It listed the files, read the same two files again, and ran nothing. In two runs, both passes approved a test that fails when run: it uses `pytest.raises` without importing `pytest`. A second model reading has the same blind spot as the first one.

## Decision

A Validate pass that approves ends the run. There is no second, confirming pass.

Everything else about Validate stays: a failing pass sends work back (DR-005), a pass that checked nothing is run again once, and check scripts from an earlier pass are run again by the next pass after a loopback.

## Consequences

### Positive

- Every approved `craft` and `forge` run is one Validate pass shorter, about 35–50 seconds on qwen3:8b.
- Fewer tokens spent and less context used per run.

### Negative

- A rule a single Validate pass misses is no longer given a second chance. That was the case the second pass was added for. It was seen on large benchmark tasks, and this change was measured only on a small one.
- The real gap remains: Validate can mark work Verified without running anything. A second reading did not close it. Running the project's tests, or refusing a Verified that no executed check backs, is the fix to pursue, as separate decisions.

## Alternatives considered

- **Keep the second pass only for larger changes.** Keeps a reading for the case it was added for. Rejected for now: there is no measurement showing the second pass helps at any size, and a size threshold would be a guess.
- **Keep it, and make it opt-in in config.** Adds a setting with no evidence that anyone needs it. It can be reintroduced with a measurement if large tasks show missed rules.
