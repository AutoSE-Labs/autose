# DR-072: One Short Validate Prompt, and the Output of Commands Run After the Last Edit

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** `VALIDATE_PROMPT` in `src/logic/workflows/prompts.py`, `VALIDATE_LEDGER_INSTRUCTION` in `src/logic/requirements.py`, `RequestMemory` in `src/logic/common/memory.py`, `commands_section` in `src/logic/workflows/run_changes.py`, and the execute stage's `run_command` and the validate stage's input in `src/logic/workflows/staged.py`. Applies to `craft` and `forge`. DR-005's fail-closed verdict, DR-008's read-only Validate and DR-021's configured prompts are unchanged.

## Context

**The Validate prompt.** Validate's instructions were about 1,600 tokens, three times Execute's. Most of that was a checklist added on 2026-10-18 (AutoSE Pro `98b4411`, ported here as `03c318b`) after DeepSWE runs with a local 27B model: run every default, every listed value, every spelling of an input, optional parts alone and together, nesting, round trips, and compare new values with existing ones. The commit lists it as one line among ten unrelated fixes, and no decision record or measurement backs it. Each bullet reads as the fix for one failed task. Most requests have none of these cases, but the model reads all of them on every Validate pass. A small model is more likely to be confused by rules that do not apply than helped by them, and a larger model checks a default or a nested case unprompted when the request is about one. Each extra check is also another model turn, at least 4 seconds on a local model.

**The test output.** DR-005 lets Validate mark a requirement `Verified` only for something it confirmed itself; Execute's report is the model's own account, so it does not count. On qwen3:8b, with a small `craft` request (make `divide()` raise `ValueError` on zero, and add a test), Execute ran `python -m pytest test_calc.py` after its edits in every run and got `2 passed`, exit code 0. AutoSE captured that output and then dropped it. Validate saw the diff (DR-071) but not the test result, so its first pass often answered at once without running anything and marked both requirements `Unverified`, and the runner started a whole second Validate pass. In 6 runs on 2026-11-13 that happened 3 times, with the old instructions and with DR-071's, and cost 24–43 seconds each time. In another run Validate ran `git_diff` twice and `git_log` twice to find the change it had already been given, then ran pytest again: 7 turns and 42 seconds to confirm a result AutoSE already held.

## Decision

**Validate has one short prompt, for every change.** It keeps what applies to any change and drops the checklist:

- the request is the specification: check each thing it explicitly asks for, and fail anything asked for and missing;
- check the way a user would use it by default, with no optional settings;
- fail anything broken or removed that worked before, or that contradicts a recorded decision;
- tests the implementation stage wrote reflect its own reading of the request, so check yourself any statement they leave unchecked;
- the diff and the command output are already in the input, so do not call `git_status` or `git_diff` to find the change, and do not run a command again whose output is there (in 2 of the first 4 runs with the command output, Validate still did both, and spent 4 turns on it);
- run the project's tests if their output is not already there, and write any check of your own as a script in `.autose-scratch/`;
- mark `Verified` only for what was confirmed, never on the implementation stage's word, and when sending work back, say exactly what must change.

Its own rules are about 2,300 characters, against 4,900 before. The separate short prompt DR-071 added for small changes is no longer needed and is removed, so every change gets the same instructions. A configured `validate` prompt (DR-021) still replaces it.

**AutoSE keeps the output of each command Execute runs to the end, until the next write.** The execute stage's `run_command` is wrapped like its write tools (DR-008's `instrument`). A result that ends in its exit code is kept with its command. A refusal, a timeout or a command left running in the background has no final output and is not kept. Any accepted write clears what was kept, because a command run before it describes code that has changed. At most the last 3 commands are kept, with the last 2,000 characters of each, where a test runner prints its summary and exit code.

**Validate gets them under "Commands run after the last edit".** The section says AutoSE recorded the output, that no write tool changed anything after, and that Execute chose the commands and may have written the tests they run, so Validate still judges whether they check what the request asks. The rules say a passing test there that exercises a requirement verifies it: cite that command as the evidence and do not run it again. The ledger rule now allows `Verified` from "the diff or command output AutoSE put in your input".

## Consequences

### Positive

- Validate can verify a change in one pass from evidence already in hand, instead of a pass that marks everything `Unverified` and a second pass that re-checks it. Results of the real runs are in the commit message.
- A failing test run is in front of Validate too, which is a defect it can name without running anything.
- Every change gets the same instructions: one prompt to maintain, and a model that reads only rules that apply.

### Negative

- Large benchmark tasks lose the option-space checklist. A task whose failure one of its bullets was written for may fail again. This is to be measured on DeepSWE-20 with the 27B model, old prompt against new; a bullet comes back only if a task it would have caught regresses.
- Validate's input is longer by up to 3 commands' output, at most about 6,000 characters.
- A shell command can change files without a write tool, and such a change does not clear the kept output. Its output still comes from after every tracked edit; the section says only that no write tool changed anything since.
- Validate may accept a test Execute wrote that checks too little. The diff shows the test, and the rules tell Validate to check what such tests leave out.

## Alternatives considered

- **Keep the checklist for large changes only (DR-071 as first written).** Two prompts to maintain, for a benefit nobody has measured, and the large tasks are where a confused model costs most.
- **AutoSE runs the project's tests itself after Execute.** Stronger evidence, but AutoSE first needs a reliable way to find each project's test command. This change uses the run Execute already made, and does not stop that being added later.
- **Pass only commands that look like tests.** Telling a test command from any other is guesswork across languages and tools. Validate can read the command.
- **Keep every command of the stage.** Output from before the last edit describes code that is gone, and passing it on invites Validate to approve a stale result.
