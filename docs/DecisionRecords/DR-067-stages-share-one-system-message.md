# DR-067: Stages Share One System Message; Each Stage's Rules Lead Its First User Message

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/workflows/prompts.py`, `StageAgent` in `src/logic/workflows/stages.py`, `BaseAgent` in `src/logic/common/agent.py`. No change to what any stage is told or which tools it has (DR-004, DR-008).

## Context

A `craft` task runs Plan, Execute and Validate, and each stage starts a new conversation (DR-004). Before writing anything, a model reads its whole input. Local runtimes such as Ollama and llama.cpp remember what they last read and skip any part of a new request that starts the same way, so within a stage each round reads only the new lines.

Each stage's prompt was sent whole as the system message: a shared opening, then the stage's own rules. Chat templates such as Qwen's put the tool list after the system message. The stage rules differ, so every stage's input stopped matching the previous stage's a few lines in, and the runtime re-read everything after that point, including the tool list. The tool list is the largest part of a stage's opening (about 1,400–1,900 tokens of 2,000–2,900), and it is nearly the same in every stage: Plan's ten tools come first, in the same order, in Execute's and Validate's.

On qwen3:8b on an Apple M4 (16 GB), reading runs at about 150–170 tokens a second, so each stage switch spent 11–19 seconds re-reading its opening.

## Decision

The system message is the shared opening only (`SHARED_PROMPT`), identical for every stage. Each stage's own rules are placed at the start of its first user message, ahead of the stage input (the request, the plan, the ledger). The same applies to the one-off repair call (`repair_output`).

A prompt configured under DR-021 is split the same way when it begins with the shared opening. One that does not is sent whole as the system message, as before.

## Consequences

### Positive

- Every stage's input now starts with the same system message and the same tool list, so the runtime reuses what it read for the previous stage. Measured on qwen3:8b with Ollama, reading the three stage openings took 43.9 seconds in the old layout and 30.1 seconds in the new one (Execute 13.6s → 6.8s, Validate 19.3s → 11.0s).
- No stage is told anything different, sees anything more, or gets a different tool. DR-004's clean memory per stage and DR-008's tool lists are unchanged.
- The stage rules still come before the task, as `prompts.py` requires ("state the output shape before the task").

### Negative

- The stage rules are no longer in the system role. A model may weigh them differently there. This has to be checked on real runs, not assumed. In the first real `craft` run on qwen3:8b (one run each way, same small task), Execute reached its first action in 19.9 seconds instead of 28.7, and both layouts made the same changes. The new-layout run was slower overall (245 seconds against 161) because the model chose differently: it guessed wrong paths and redid an edit. Its first Validate pass also answered without checking anything and had to be sent back, which the old-layout run did not do. One run cannot say whether that came from the move or from chance, so it is watched in later runs.
- The saving depends on the runtime reusing a shared start. A hosted API without prefix caching gains nothing, and loses nothing either.
- A hand-written prompt that does not start with the shared opening gets no saving.

## Alternatives considered

- **Hand the files Plan read to Execute.** Saves the repeated file reads as well, but changes what each stage knows and makes Validate less independent. It is a separate decision, if it is made at all.
- **Put the tool list before the system message.** The order is set by the model's chat template on the runtime, not by AutoSE.
- **Send the stage rules as a second system message.** Several chat templates merge or reject a second system message, so where it lands would vary by model.
