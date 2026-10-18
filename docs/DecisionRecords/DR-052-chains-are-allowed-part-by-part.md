# DR-052: A Chain of Commands Is Allowed Part by Part

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/common/allowances.py` (`_split_chain`, `_chain_patterns`, `AllowanceOffer.also`/`every`/`label`, `standing_for`); the bridge's approval payload, `resolve_approval`, `add_allowance` and `_ask`; `TerminalApprovalGate`; the "Always allow" button in `app.js`. Refines DR-048 and DR-050.

## Context

DR-048 made any command with `&&` or `;` once-only. The model writes chains
all the time, most often `cd <workspace> && python script.py`, so the same
harmless call was put to the person again and again, and "Always allow" was
missing exactly where it was wanted.

## Decision

- A command is cut at `&&`, `||`, `;` and newlines outside quotes. `cd` with
  at most one argument is neutral. Every other part is judged by DR-048 and
  DR-050 on its own (output shaping set aside, wrappers, destructive commands,
  inline code and so on).
- If any part can only be allowed once, the whole chain can only be allowed
  once, and the note names that part ("one part (echo) writes to a file (>)").
- Otherwise "Always allow" grants one allowance per distinct part
  ("Always allow `pip install …` and `pytest …` here"), and a later call runs
  unasked only when **every** part of it is already allowed (`standing_for`).
- A rule written by hand stays one command; a chain is added part by part.
- The once-only note names what the syntax does (writes to a file, pipes into
  another program, runs a command inside it) instead of listing every symbol.

## Consequences

### Positive

- `cd app && python gen_docs.py` asks once; the chains the model writes stop
  interrupting.

### Negative

- `cd` can point a later allowed command at another directory. The allowed
  command words are the same, and the tools' workspace boundary is unchanged,
  but the directory a command runs in is not part of the allowance.
- Quoted text that contains shell syntax (`echo 'a && b'`) is still treated as
  syntax and stays once-only: a conservative miss, not a hole.
