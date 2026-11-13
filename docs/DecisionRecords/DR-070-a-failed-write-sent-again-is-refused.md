# DR-070: A Failed Write Sent Again Is Refused, and a Third Send Ends the Call

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** the agent loop in `src/logic/common/agent.py`. Applies to every stage that holds `write_file` or `edit_file`. DR-030's no-progress guard and the read-only repeat guard are unchanged.

## Context

The agent loop already refuses a read-only call made a third time with the same arguments, and ends a call after six rounds in a row that learn nothing new (DR-030). Writes were covered only by the six-round guard. A failed write changes no file, so the same arguments fail the same way, and the model is told the same error each time.

On qwen3:8b, three of six saved `craft` runs had Execute send one failing edit over and over with byte-identical arguments: seven times in a row in one run; six times in another, with re-reads of the file in between; and in a third, two different failing edits sent twice each. The re-reads returned nothing new, so only the six-round guard ended each loop. With the request for a final answer that follows it, each loop cost about 1.5–2 minutes.

## Decision

The loop records each write that fails, keyed on its tool and arguments, the same way the read-only repeat guard keys on them.

- **First send:** it runs and fails as before.
- **Second send:** it is not run. The model is told: "Refused: you already sent this exact edit_file call for test_calc.py and it failed. No file has changed since, so it would fail the same way, and it was not run. Do not send it again: re-read the file and send a different call, or move on to your next step." A `tool_call_repeated` event is emitted.
- **Third send:** it is refused the same way, and the call ends after that round as a stall does. An `agent_stalled` event names the write, and the model is asked once for its answer with tools removed, so the stage still reports what it did. Validate then sees what is missing and can send the work back.

The record is cleared when any write succeeds and whenever a shell command runs, because either may change what the same write would now do. Reads and git queries do not clear it. A write the user denies at an approval prompt is never recorded: its result is a denial, not an error, so the user is asked again if the model resends it.

## Consequences

### Positive

- The loop is caught on the second send instead of after six rounds of no progress.
- Replayed on qwen3:8b from the point in a real run where the same failing edit was sent a second time: told the old "not found" error, the model re-read the file in 5 of 5 tries, which in the real run led to sending the edit again. Told the refusal, it moved on in 5 of 5 tries. Results of the full Execute stage continued from that point, old code against new, are in the commit message.
- The refusal states only facts the loop has checked: the same call failed before, and no file has changed since.

### Negative

- A write that failed for a reason outside the workspace, and would now succeed, is refused once. No such case exists among the write tools today: their failures depend only on the arguments and the files.
- A failing write sent twice with a different, equally wrong argument is not caught by this guard. The six-round guard still catches it.

## Alternatives considered

- **Lower the six-round guard.** DR-030 records that three rounds cut planning and validation short. A tighter guard on the one case that is certain to be wasted does not have that cost.
- **A more detailed "not found" error.** Replayed on the same model, an error saying exactly where `old_str` stopped matching made the model resend the same edit in 5 of 5 tries, against 0 of 5 for today's error. It was dropped and not built.
- **End the call on the second send.** The refusal alone got the model moving in the replay, and ending the call costs a forced answer and usually a Validate round. One refusal first is cheaper when it works.
