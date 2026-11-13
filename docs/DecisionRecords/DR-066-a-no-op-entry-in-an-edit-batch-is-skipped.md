# DR-066: A No-Op Entry in an Edit Batch Is Skipped, Not Fatal

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/tools/editing.py`. Amends one point of DR-007; the rest of DR-007 stands.

## Context

DR-007 rejects a whole `edit_file` batch, leaving the file unchanged, if any entry is empty, ambiguous, overlapping, not found, or a no-op (`old_str` equal to `new_str`).

On qwen3:8b running locally, a `craft` request to make `divide()` raise `ValueError` on zero sent this batch:

```json
[{"old_str": "def divide(a, b):", "new_str": "def divide(a, b):\n    if b == 0:\n        raise ValueError(...)"},
 {"old_str": "    return a / b", "new_str": "    return a / b"}]
```

The first entry is the fix. The second is the model saying "keep this line". The tool answered `Error: edits[1] would make no change.`, the model resent the identical batch seven times, and the stall guard ended Execute about a minute later with the file untouched. The whole six-minute run produced no change.

Small local models list unchanged context this way often. They are the models AutoSE is built for (DR-015).

## Decision

An entry whose `old_str` equals its `new_str` is skipped. The other entries are validated and applied exactly as DR-007 describes, and the result names the skipped entries: `Edited 1 block(s) in calc.py (skipped edits[1]: old_str and new_str were identical, so there was nothing to change — do not resend it)`.

A batch made up only of such entries is still an error, because the model believes it changed the file and has to be told it did not. The error says whether that text is already in the file. If it is, it tells the model that if this is the result it wants, the edit is already done and it should go on; if it meant to change the text, it must send a `new_str` that differs. If the text is not in the file, the error says so and asks for a `new_str` that differs.

This was added after a later qwen3:8b run. Execute made its edit, then sent the finished text back as both `old_str` and `new_str` to check it had landed. The error `would make no change` did not tell it the text was there. It read the error as a failure and went back to resending its original edit, which was then "not found" because it had already been made. It alternated between the two until the stall guard ended the stage, about two minutes later. Whether the text is in the file is a fact the tool can state, and stating it does not claim anything the tool cannot see.

Every other problem still rejects the whole batch.

## Consequences

### Positive

- The fix in a batch lands even when the model pads it with "keep" entries, so a correct edit is not turned into a loop.
- The file content is identical to what applying the no-op would give. Nothing the model meant to change is lost or partly applied.

### Negative

- A no-op entry is no longer checked for matching once. If its `old_str` does not occur in the file, nothing reports it. It changed nothing either way, so nothing is written wrongly.

## Alternatives considered

- **Keep rejecting, with a clearer error.** The error already said which entry was the problem. The model resent it anyway. A small model does not reliably act on that kind of message.
- **Skip the no-op only if it matches exactly once.** This adds a failure the model cannot see the point of, for an entry that changes nothing.
