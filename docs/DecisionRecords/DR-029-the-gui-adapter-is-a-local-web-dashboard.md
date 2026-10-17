# DR-029: The GUI Adapter Is a Local Web Dashboard, Served by the Standard Library

- **Status:** Accepted
- **Date:** 2026-08-31
- **Decision owners:** AutoSE maintainers
- **Scope:** `src/gui/`, `python -m gui`. **Implements DR-013's differentiator; exercises DR-002; inherits DR-012, DR-024, DR-026.**

## Context

DR-013 makes the GUI the one thing that differs between the public and Pro
editions, and DR-002 places it in `src/gui/` as a thin adapter over
`logic.session.Session`. Neither says what the GUI *is*. Until now the folder
was empty, and the earlier prototype in this project's history was a Tauri
desktop app: a Rust toolchain, a Node/Vite build, a bundled webview, a staged
Python backend, and a signed-installer story per platform.

That is a large amount of machinery to acquire in order to draw a page, and
almost none of it is about AutoSE. The interaction the interface actually needs
is modest and already fully specified by the logic layer: submit a prompt,
watch a turn happen, answer DR-012's approval question with the real arguments
in front of you, review and undo what was written (DR-024), pick a model or a
tier, and reopen a stored session (DR-026). All of that arrives as
`logic.common.events.Event` values on one channel and is answered through one
`ApprovalGate`.

Two properties had to survive whatever was chosen. The GUI must remain
*deletable* — DR-013's public edition is `rm -r src/gui`, not a build
configuration to maintain — and AutoSE's local-first promise ("by default
nothing leaves your machine") has to hold for the interface too, not only for
inference.

## Decision

`src/gui/` is a **local web dashboard**: a standard-library HTTP server that
serves one page and streams a session's events to it over Server-Sent Events.
No dependencies are added to the project, and no build step is introduced —
`gui/static/` is the page as it ships.

Concretely:

- **Two ways in, neither of them a hard dependency.** `python -m gui` is the
  package's own entry point, and `autose --web` hands off to it from the
  command people already type. No console script is declared in packaging
  metadata, and the hand-off resolves the package *by name at call time*
  (`find_spec("gui")`, then `import_module`) rather than importing it — so
  `src/tui/` carries no dependency on the adapter, a build without the folder
  answers `--web` with one sentence instead of a traceback, and deleting the
  folder leaves nothing dangling. `tests/test_gui.py` skips itself when the
  package is absent and asserts that no `[project.scripts]` entry points into
  it; `tests/test_tui.py` asserts the flag degrades and that the shared flags
  (`--yes`, `--tier`, `--resume`, `--workspace`, `--config`) are forwarded
  rather than reimplemented.

  A static `import gui` in `src/tui/` remains forbidden and is checked by
  `tests/test_architecture.py`. The distinction is the whole of the rule: the
  terminal adapter may *name* the dashboard, and may not *depend* on it.
- **`gui/bridge.py` owns the whole HTTP-to-session translation.** A turn runs
  on a worker thread; frames are numbered and briefly retained so a browser
  that reloads mid-turn replays what it missed; a second prompt during a
  running turn is refused with a conflict rather than raced, because `Session`
  is not concurrent.
- **Approval is fail-closed in the browser too (DR-012).** A gated call parks
  until someone answers it and is *denied* on timeout, so a closed tab cannot
  leave a shell command pending, and an absent browser cannot let one through.
- **Local trust is enforced, not assumed.** Loopback by default; a per-run
  token the browser must present; the `Host` header must name a host we serve
  (which is what stops DNS rebinding); a cross-origin `Origin` is refused. The
  dashboard drives an agent that writes files and runs approved commands —
  "it's only localhost" is not a security model for that.
- **The page fetches nothing.** No CDN script, no Google Fonts, no markdown
  library: the type stack is the system one, and the small markdown renderer
  and diff view are in `gui/static/app.js`. A test asserts no absolute URL
  appears in the shipped assets, and the responses carry a
  `default-src 'self'` policy.
- **The live card is updated in place, not re-rendered.** A stage's output
  arrives as deltas and the card repaints on every animation frame, so the
  frame is built once and each part written individually (`data-part`, guarded
  by a last-value check). Assigning to the card's own `innerHTML` there — which
  it did at first — destroys and re-creates the streaming answer block several
  times a second: it collapses to nothing and re-expands, the scroll position
  goes with it, and the CSS animation on the status dot restarts. What that
  looks like is the last line of the answer flickering as it streams.
  `tests/test_gui.py` asserts `renderRun` does not touch `innerHTML`.
- **The design is the one from the desktop prototype** — bone canvas, ink
  navy, signal orange, the blueprint grid, crop-marked cards, the stage
  pipeline and the activity feed — so the two interfaces read as one product.

## Consequences

### Positive

- The GUI costs the project zero dependencies, zero build tooling, and no
  per-platform packaging. It runs anywhere the CLI runs.
- It is genuinely separable: the suite is green with `src/gui/` deleted, which
  is the only form of DR-013's claim anyone can check.
- A second *rendering* adapter over the same facade, alongside DR-027's JSON
  one, keeps DR-002 honest for interactive concerns too — streaming, approval,
  undo — not only for batch output.
- The interface is reachable from a phone or another machine on the LAN by
  binding a different host, deliberately and with a warning, rather than never.

### Negative

- A browser tab is not a desktop app: no native window, no menu bar, no
  installer, no file dialogs. If the product later needs those, this decision
  is superseded rather than extended — though a shell around this same page is
  the cheap version of that.
- **There was no stop button.** `logic` had no cancellation seam: a turn was
  bounded by DR-009's time budget and loopback cap, and nothing offered to
  interrupt it early. Inventing one in the GUI would either have been a lie or
  a behaviour change in `logic` belonging in its own DR — which is what
  [DR-030](DR-030-unlimited-by-default-caps-and-a-user-held-stop.md) then made
  it. The dashboard now has a real Stop button over that seam
  (`POST /api/interrupt`), which is what a cooperative token bought that an
  exception could not: the turn runs on a worker thread and the request arrives
  on the HTTP thread.
- Hand-written vanilla JS and CSS is more code in the repository than a
  framework would be, and it is not type-checked the way `src/` is.
- The token lives in the URL, so it appears in browser history on that machine.
  Acceptable for a per-run credential to a loopback service; it is not a
  password and does not outlive the process.

## Alternatives considered

### Rebuild the Tauri desktop app

Rejected for now. It is the right answer only once a native shell earns its
keep; today it would add a Rust and Node toolchain, a build pipeline, and
three installer stories to a project whose entire dependency list is two
packages — to render the same page this decision serves directly.

### A web framework (FastAPI/Flask) plus a bundled frontend

Rejected. It is a smaller cost than Tauri, and still the wrong trade for one
page and eleven endpoints: it puts a dependency tree and a build step between a
contributor and a change to the interface, and a "local-first" tool that
installs a web stack to draw its own UI has taken on a networked application's
supply chain for nothing.

### A flag on the existing CLI (`autose --web`) — adopted, with a constraint

Initially rejected on DR-013 grounds, and that rejection was too broad: it
assumed the flag required `src/tui/cli.py` to import `gui`. It does not. The
flag resolves the package by name at call time, which keeps the import graph
one-directional and makes the public edition's behaviour explicit ("the web
dashboard ships in the Pro edition") instead of a `ModuleNotFoundError`.

What stays rejected is the static import, and a console script (`autose-web =
"gui.cli:main"`): packaging metadata is declared at build time and cannot
degrade, so a script entry point would survive the folder's deletion and fail
when invoked. `python -m gui` remains available and is what the dashboard's own
documentation uses.

### No token, trusting loopback

Rejected. Any process or user on the machine could then drive the agent inside
the workspace, and a page in the user's own browser could post prompts to it.
The token and the `Host`/`Origin` checks cost a few lines and remove that
class of problem.
