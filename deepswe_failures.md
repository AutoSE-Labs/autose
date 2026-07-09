# DeepSWE Failure Review

Run reviewed: `/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose`

This note reviews 10 completed tasks from the current DeepSWE run and records what happened, where the run went wrong, and whether the issue looks like model capability, harness behavior, or infrastructure.

## Summary

Across these 10 tasks, the dominant failure mode is not verifier breakage. The agent usually reaches the verifier, and many tasks get substantial partial credit. The main problems are:

- The agent often understands the area of code but exits `CODE` with no surviving diff.
- The `TEST` stage often re-explores the repo or runs weak commands instead of validating a concrete candidate fix.
- Some edited tasks produce large speculative patches that miss the task boundary and collapse `p2p`.
- Stage summaries in `result.json` are often malformed tool-call tokens or timeout placeholders rather than useful summaries.
- There is also at least one infrastructure failure unrelated to model reasoning: Docker image pull rate limiting.

## Timeout Note

The current DeepSWE run is not operating with a 30-minute agent cap.

- The checked-in DeepSWE task configs use `agent.timeout_sec = 5400` seconds and `verifier.timeout_sec = 1800` seconds.
- The 10 reviewed tasks all finished well under 30 minutes anyway, ranging from about `6.2` minutes to `26.8` minutes.
- For these reviewed tasks, the current failures are not explained by a 30-minute cutoff.

## Gold Solution Scale

DeepSWE ships a reference patch for each task at `solution/solution.patch`. Comparing our run outputs to those patches changes the interpretation in an important way:

- The average gold patch across the benchmark is large: about `28.6 KB`, `7.4` files, and `668` added lines.
- Several of the no-edit failures were tasks whose gold patches are also large:
  - `arcane-drift-detection-baselines`: gold `44,828` bytes, `14` files, `1,083` added
  - `bandit-interprocedural-taint-checks`: gold `32,476` bytes, `9` files, `842` added
  - `skrub-duration-encoding`: gold `31,594` bytes, `7` files, `834` added
- That means those tasks are not just “the harness failed to persist a tiny obvious fix.” In those cases, the model also failed to reach the scale of implementation the task actually required.

## Per-Task Findings

### 1. `arcane-drift-detection-baselines__CVCsHzL`

- Outcome: `reward=0`, `partial=0.0238`, `p2p=1.0`, `diff_after_code=0`, `committed=False`
- Gold solution scale: `44,828` bytes, `14` files, `1,083` added, `0` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/arcane-drift-detection-baselines__CVCsHzL/result.json)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/arcane-drift-detection-baselines__CVCsHzL/agent/autose-deepswe.log)
- What happened:
  - The agent explored model and migration files.
  - `CODE` ended with `diff after code: 0 chars`.
  - In `TEST`, it wrote Go model files and SQLite migration files, but the run still ended with `commit skipped: no diff`.
- Where it went wrong:
  - The run mixed implementation work into `TEST`.
  - The writes performed late in the run did not survive into the final patch.
  - This is still a harness/control problem, but the gold patch size shows it is also a substantial model-capability miss. The task needed a large multi-file implementation and the run ended with nothing usable.

### 2. `bandit-interprocedural-taint-che__2vMaTRX`

- Outcome: `reward=0`, `partial=0.8162`, `p2p=1.0`, `diff_after_code=0`, `committed=False`
- Gold solution scale: `32,476` bytes, `9` files, `842` added, `1` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/bandit-interprocedural-taint-che__2vMaTRX/result.json)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/bandit-interprocedural-taint-che__2vMaTRX/agent/autose-deepswe.log)
- What happened:
  - The agent read the taint analyzer and SQL injection plugin.
  - It attempted several `write_file` calls during `CODE`, but still ended with `diff after code: 0 chars`.
  - In `TEST`, it created `test_injection.py` and ran `bandit test_injection.py`.
- Where it went wrong:
  - The task got high partial credit without any surviving patch, which means the model found relevant code but never landed a repo change.
  - `TEST` was used for ad hoc experimentation rather than validating a committed fix.
  - `code_summary` is `[LLM error: timed out]`, which suggests the stage output discipline is weak under pressure.
  - The gold patch size also shows that the true task is materially larger than the run behavior suggests. This is a combined harness and model-scale failure, not just a missing final commit.

### 3. `cattrs-partial-structuring-recov__STykbos`

- Outcome: `reward=0`, `partial=0.1316`, `f2p=0.0435`, `p2p=1.0`, `diff_after_code=1939`, `committed=True`
- Gold solution scale: `24,810` bytes, `3` files, `632` added, `0` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/cattrs-partial-structuring-recov__STykbos/result.json)
  - [model.patch](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/cattrs-partial-structuring-recov__STykbos/artifacts/model.patch)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/cattrs-partial-structuring-recov__STykbos/agent/autose-deepswe.log)
- What happened:
  - The agent introduced a new `PartialResult` dataclass into `src/cattrs/converters.py` and exported it from `src/cattrs/__init__.py`.
  - It ran `pytest`, `pytest tests/test_converter.py`, and `pytest tests/test_baseconverter.py`.
- Where it went wrong:
  - This is a real patch, but it looks like API surface scaffolding rather than a task-complete behavioral implementation.
  - The patch is narrow enough to preserve `p2p=1.0`, but it only moves a small fraction of failing tests.
  - Against the gold patch, our output is dramatically undersized: about `1.9 KB` and `19` added lines versus `24.8 KB` and `632` added lines. This is mostly under-implementation.

### 4. `etree-xml-diff-patch__u3QXyLG`

- Outcome: `reward=0`, `partial=0.0`, `f2p=0.0`, `p2p=0.0`, `diff_after_code=3156`, `committed=True`
- Gold solution scale: `45,435` bytes, `8` files, `1,647` added, `0` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/etree-xml-diff-patch__u3QXyLG/result.json)
  - [model.patch](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/etree-xml-diff-patch__u3QXyLG/artifacts/model.patch)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/etree-xml-diff-patch__u3QXyLG/agent/autose-deepswe.log)
- What happened:
  - The agent added new files such as `diff.go` and broad XML diffing data structures.
  - It also wrote a new Go test file and ran `go test -v etree_new_test.go etree.go diff.go merge.go` and `go test -v .`.
- Where it went wrong:
  - This patch is broad and speculative. It introduces a lot of new surface area for a task that likely needed a more constrained change.
  - `p2p=0.0` indicates the patch regressed the existing test baseline badly.
  - The gold patch is also large, so the problem is not “the task was actually small.” The issue is that the model attempted the wrong large implementation.

### 5. `fastapi-deprecation-response-hea__UzjWJHY`

- Outcome: `reward=0`, `partial=0.9581`, `p2p=1.0`, `diff_after_code=2064`, `committed=True`
- Gold solution scale: `45,447` bytes, `4` files, `784` added, `1` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/fastapi-deprecation-response-hea__UzjWJHY/result.json)
  - [model.patch](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/fastapi-deprecation-response-hea__UzjWJHY/artifacts/model.patch)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/fastapi-deprecation-response-hea__UzjWJHY/agent/autose-deepswe.log)
- What happened:
  - The patch modified `fastapi/routing.py` to add deprecation-related headers such as `Deprecation`, `Sunset`, and `Link`.
  - It preserved baseline behavior well enough to keep `p2p=1.0`.
- Where it went wrong:
  - This is a near-miss: the patch is plausible and non-destructive, but it still did not satisfy the failing tests.
  - The likely problem is semantic mismatch or missing edge-case coverage rather than total misunderstanding.
  - The gold patch is much larger than our patch, which means this task required much more propagation and supporting work than the run attempted. This is a clear under-scoped implementation.

### 6. `gql-incremental-graphql-delivery__C933zqD`

- Outcome: `reward=0`, `partial=0.0`, `p2p=0.0`, `diff_after_code=1781`, `committed=True`
- Gold solution scale: `24,744` bytes, `7` files, `537` added, `6` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/gql-incremental-graphql-delivery__C933zqD/result.json)
  - [model.patch](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/gql-incremental-graphql-delivery__C933zqD/artifacts/model.patch)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/gql-incremental-graphql-delivery__C933zqD/agent/autose-deepswe.log)
- What happened:
  - The agent introduced a new `gql/incremental.py` module and changed `AsyncTransport` to add `execute_incremental`.
  - It ran only `grep -n "execute_incremental" gql/client.py` after patching.
- Where it went wrong:
  - This is another speculative architecture change with almost no real validation.
  - The test step did not run any task-relevant suite, so there was no meaningful feedback loop after the code change.
  - `p2p=0.0` indicates the patch likely broke baseline expectations outright.
  - The gold patch is also much larger than the run patch, so this is both wrong-direction reasoning and under-scoped implementation.

### 7. `langchain-request-coalescing__Wyy97N6`

- Outcome: `reward=0`, `partial=0.9397`, `f2p=0.66`, `p2p=1.0`, `diff_after_code=1940`, `committed=True`
- Gold solution scale: `28,652` bytes, `4` files, `820` added, `0` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/langchain-request-coalescing__Wyy97N6/result.json)
  - [model.patch](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/langchain-request-coalescing__Wyy97N6/artifacts/model.patch)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/langchain-request-coalescing__Wyy97N6/agent/autose-deepswe.log)
- What happened:
  - The agent added request-coalescing support, including a new `coalesce.py` module and `with_coalesce()` on `Runnable`.
  - The patch is substantial but non-destructive enough to preserve `p2p=1.0`.
- Where it went wrong:
  - This is another near-miss with real progress: `f2p=0.66` means many failing tests moved in the right direction.
  - The remaining gap is likely missing semantics, edge cases, or integration points.
  - `code_summary` is `[LLM error: timed out]`, which again shows the harness is not reliably producing clean stage conclusions even when the patch itself is useful.
  - Compared to the gold patch, this is one of the closer runs. Our patch is still smaller, but it is at least in the right scale class, which supports the “stronger model could finish this” reading.

### 8. `onedump-dump-encryption-pipeline__hx9W2vz`

- Outcome: `reward=0`, `partial=0.7045`, `f2p=0.7561`, `p2p=0.0`, `diff_after_code=5833`, `committed=True`
- Gold solution scale: `21,333` bytes, `11` files, `484` added, `41` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/onedump-dump-encryption-pipeline__hx9W2vz/result.json)
  - [model.patch](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/onedump-dump-encryption-pipeline__hx9W2vz/artifacts/model.patch)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/onedump-dump-encryption-pipeline__hx9W2vz/agent/autose-deepswe.log)
- What happened:
  - The patch added a new encryption config and implementation with large changes in `config/job.go` and a new `encryption/encryption.go`.
  - It ran `go test -v encryption/encryption_test.go encryption/encryption.go` and `go test -v ./encryption`.
- Where it went wrong:
  - This patch clearly captured much of the task intent, given `f2p=0.7561`.
  - But it also destroyed the preserved baseline, with `p2p=0.0`.
  - This is not an undersized patch. Our patch is actually larger than the gold patch, so the failure mode is wrong or overbroad implementation rather than lack of effort.

### 9. `pwntools-tube-multiplexing__ztHAobb`

- Outcome: no verifier result; infrastructure exception before normal completion
- Gold solution scale: `23,556` bytes, `4` files, `622` added, `1` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/pwntools-tube-multiplexing__ztHAobb/result.json)
- What happened:
  - The environment failed during Docker compose setup.
  - The recorded exception shows an ECR pull failure: `toomanyrequests: Rate exceeded`.
- Where it went wrong:
  - This is not a model failure.
  - This is an infrastructure/harness-side issue caused by remote image pull rate limiting.
  - It should be counted separately from reasoning failures.

### 10. `skrub-duration-encoding__km7iWru`

- Outcome: `reward=0`, `partial=0.9554`, `p2p=1.0`, `diff_after_code=0`, `committed=False`
- Gold solution scale: `31,594` bytes, `7` files, `834` added, `2` removed
- Artifacts:
  - [result.json](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/skrub-duration-encoding__km7iWru/result.json)
  - [autose-deepswe.log](/home/sustaind/autose/benchmarks/results/deepswe/full_20260705_120933_autose/skrub-duration-encoding__km7iWru/agent/autose-deepswe.log)
- What happened:
  - The agent read the right family of files for duration handling.
  - `CODE` ended with `diff after code: 0 chars`.
  - `TEST` re-read relevant modules and ran only `python -c "import polars; print(polars.__version__)"`.
- Where it went wrong:
  - This is the cleanest example of the current standard-flow weakness.
  - The model found the right code area, but never crossed from understanding into editing.
  - The `TEST` stage did not validate a proposed fix; it drifted back into repo inspection and a weak environment command.
  - The gold patch size also makes this a clear capability problem: the real fix is large, multi-file, and far beyond what the model committed to in this run.

## Cross-Cutting Failure Modes

### 1. No surviving patch after substantial exploration

Seen clearly in:

- `arcane-drift-detection-baselines__CVCsHzL`
- `bandit-interprocedural-taint-che__2vMaTRX`
- `skrub-duration-encoding__km7iWru`

Pattern:

- The model reads many relevant files.
- Sometimes it even issues `write_file` or `edit_file` calls.
- `CODE` still ends with `diff after code: 0 chars`.

Interpretation:

- This is partly a model indecision problem.
- It is also a harness-control problem because the pipeline does not force `CODE` to finish with a concrete candidate patch once enough context has been gathered.
- After comparing to gold patches, these are no longer best described as purely harness misses. Several of them required large multi-file implementations, so the model also failed to scale up to the actual task.

### 2. `TEST` stage wandering

Seen clearly in:

- `bandit-interprocedural-taint-che__2vMaTRX`
- `gql-incremental-graphql-delivery__C933zqD`
- `skrub-duration-encoding__km7iWru`
- `arcane-drift-detection-baselines__CVCsHzL`

Pattern:

- `TEST` frequently performs more reading or one-off experiments.
- It does not consistently run a task-directed validation command tied to the produced diff.

Interpretation:

- This is primarily a harness/prompting issue.
- `TEST` needs the original issue, plan, diff summary, and a candidate test command, and it should stay constrained to validating that patch.

### 3. Broad speculative implementations that hurt `p2p`

Seen clearly in:

- `etree-xml-diff-patch__u3QXyLG`
- `gql-incremental-graphql-delivery__C933zqD`
- `onedump-dump-encryption-pipeline__hx9W2vz`

Pattern:

- The model adds new modules or major new abstractions.
- The patch appears reasonable at a high level.
- Baseline-preserving tests collapse.

Interpretation:

- This is mostly a model capability issue.
- The model is attempting greenfield design instead of a repo-grounded, minimal, test-driven fix.

### 4. Near-miss patches with strong partial credit

Seen clearly in:

- `fastapi-deprecation-response-hea__UzjWJHY`
- `langchain-request-coalescing__Wyy97N6`
- `onedump-dump-encryption-pipeline__hx9W2vz` on the failing-test side only

Pattern:

- The patch is real and non-empty.
- `p2p` is preserved or much of `f2p` improves.
- The solve still does not cross the final threshold.

Interpretation:

- These are the strongest cases for model-quality limits.
- A better model might convert these from partial to full solves without major harness changes.
- The gold comparisons sharpen this split:
  - `langchain` looks comparatively close in both direction and patch scale.
  - `fastapi` preserved behavior and got high partial, but its patch is far smaller than the gold implementation, so some of the miss is simply under-scoping.

### 5. Malformed stage outputs

Seen in multiple `result.json` files:

- `code_summary` and `test_report` often contain raw tool-call tokens.
- Some stages record `[LLM error: timed out]`.

Interpretation:

- This is a harness formatting/control issue.
- It makes it harder to inspect the run and may also indicate that the stage prompt contract is too weak for the model being used.

### 6. Infrastructure failure separate from agent quality

Seen in:

- `pwntools-tube-multiplexing__ztHAobb`

Pattern:

- Docker environment setup failed due to image pull rate limiting from ECR.

Interpretation:

- This should not be grouped with model or AutoSE reasoning failures.
- It is a benchmark-infrastructure reliability issue.

## Bottom Line

For these 10 tasks, the biggest repeated problem is that the current standard DeepSWE harness is not forcing a clean transition from `understand` to `edit` to `validate`. However, the edited tasks also show real model limitations:

- Some tasks never produce a surviving patch despite reading the right code.
- Some tasks produce plausible but incomplete fixes.
- Some tasks produce broad speculative implementations that break preserved behavior.
- The gold-solution comparison shows that several tasks require large multi-file implementations, and our model often either produces nothing or produces a patch far smaller than the reference solution.

So the failures are mixed:

- Harness/control issue: especially for no-edit runs and wandering test stages.
- Model capability issue: especially for the broad or incomplete patches.
- Infra issue: at least one task failed before agent reasoning due to image pull rate limits.
