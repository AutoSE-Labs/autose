# DR-069: Execute Starts With the Files the Plan Names

- **Status:** Accepted
- **Date:** 2026-11-13
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/logic/workflows/named_files.py`, the execute stage's input in `src/logic/workflows/staged.py`. Applies to `craft` and `forge`. DR-004's clean memory per stage is unchanged.

## Context

Each stage starts with a clean memory (DR-004). Execute gets the request, the plan and the requirements ledger, but not the files Plan read.

On qwen3:8b, a small `craft` request (make `divide()` raise `ValueError` on zero, and add a test) ran four times. In all four, Execute's first turn edited before reading anything. It edited `src/calc.py` and `test/test_calc.py`, though the plan said `calc.py` and `test_calc.py` and no `src/` or `test/` folder exists, and it replaced text it had invented (`assert divide(10, 2) == 5`, which is in neither file). Recovering took four to eight more turns of listing files, retrying the made-up paths, and reading. The blind first turn alone took 19–29 seconds, because the invented edit is long. In all, the guessing cost about 50–60 seconds a run.

## Decision

Execute's input includes the current contents of the files the plan names. AutoSE finds every path-like token in the plan and the ledger, keeps those that are files inside the workspace, and adds each one as `read_file` would show it, under the heading "Current contents of the files the plan names". The files are read from disk each time Execute starts, including after a loopback, so it sees them as they are, not as Plan saw them.

At most four files and 12,000 characters are included. A file that would go over the limit is left out whole, and Execute reads it itself. When the plan names no file that exists, nothing is added.

## Consequences

### Positive

- Replayed on qwen3:8b with thinking off, Execute's real first turn, using the plan from a real run, was sent five times as before and five times with the files. Without the files, all five edited `src/calc.py`, a path that does not exist. With them, all five edited `calc.py` correctly on the first turn and rewrote `test_calc.py` with the existing tests kept and a new zero-division check. The listing, failed reads and re-reads that followed every blind edit are gone.
- The files are a fact on disk, not Plan's account of them, so Execute still forms its own view of the code.

### Negative

- Execute's first input is longer, by the size of the named files (about 400 characters here, up to 12,000).
- A plan that names a file only in passing hands that file over too, within the same limits.
- A plan that names no files, or names them in a form the pattern does not catch, gets no help. Execute then works as before.

## Alternatives considered

- **Hand Execute the files Plan opened.** Plan often opens files only to understand the code, and in a large repository that can be many. The files the plan names are the ones it means to change.
- **Reply "did you mean `calc.py`?" when a path is not found.** Fixes only the path. Execute would still have edited invented text, and still spent a turn failing first.
- **Refuse an edit to a file Execute has not read in this stage.** Forces a read, so no invented text is edited, but still costs a turn per file and does nothing about guessed paths.
