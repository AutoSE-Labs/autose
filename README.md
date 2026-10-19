# AutoSE

A local-first software engineering agent. Run it against the models on your own machine — it finds Ollama, LM Studio, vLLM, or llama.cpp by itself and needs no configuration to start. Any other OpenAI-compatible endpoint works too. AutoSE never provides, bundles, or proxies a model itself, and by default nothing leaves your machine.

This is the **free edition**. It has everything an individual developer needs: the web dashboard, the terminal interface, all three tiers, approvals, `/diff` and `/undo`, and durable sessions. The **Pro edition** adds the decision-record and knowledge layer for teams: a Design stage that writes architecture decision records, retrieval and citation of a repository's decisions and an organisation's standards, and approved learned facts (DR-032).

## Install

**Get it from [autose.dev](https://autose.dev).** One line installs AutoSE, puts an **AutoSE** shortcut on your desktop, and opens the dashboard in your browser:

```bash
# macOS / Linux
curl -fsSL https://autose.dev/install.sh | sh
```

```powershell
# Windows (PowerShell)
irm https://autose.dev/install.ps1 | iex
```

The installer needs only `git`. It installs [uv](https://docs.astral.sh/uv/), which brings its own Python, so nothing else on your machine changes. Run the same line again to update. After that, open AutoSE from the desktop shortcut, or run `autose --web` in a terminal.

AutoSE runs on a model you host. If you have none yet, install [Ollama](https://ollama.com) and pull a coding model with tool calling, for example `ollama pull qwen2.5-coder:14b`. AutoSE finds it by itself.

## Usage

```bash
autose --web           # the dashboard, in your browser
autose                 # the same agent, in the terminal
autose --doctor        # what can this machine run?
```

```text
autose "why does divide() crash on zero?"          # classified spark — one pass
autose "make divide() raise ValueError, add a test" # classified craft — plan/execute/validate
autose --tier craft "restructure config loading"    # force a tier
autose --yes "run the test suite and fix failures"  # unattended: approve tool calls
autose --resume                                     # pick up where you left off
autose --json "add a test for parse_config"         # newline-delimited JSON, for scripts
```

Inside a session, `/diff` shows what it wrote and `/undo` puts it back.

## What AutoSE does

- **Local-first, bring-your-own-LLM.** AutoSE looks for a runtime already running on your machine — Ollama, LM Studio, vLLM, llama.cpp — and picks up its models, the context window each one is *actually being served in*, and whether they can call tools, with nothing configured. Any other OpenAI-compatible endpoint works too, hosted or not, by setting a base URL (DR-015). AutoSE never bundles or proxies a model.
- **The context window it reports is the one you really have.** A runtime routinely serves far less than a model supports — Ollama defaults to 4096 tokens no matter how large the model is — and an over-long request is not rejected, it is silently trimmed from the front, taking the system prompt with it. AutoSE budgets against the served window, tells you when it is smaller than the model's maximum and how to raise it, and independently checks the provider's own reported token count each round so a runtime that trims anyway is reported rather than quietly tolerated (DR-022).
- **Built for the models people actually run locally.** Reasoning models are handled properly: chain of thought is shown as it streams, kept out of the answer, and never replayed back into the context (DR-016). Output reservation scales to the model's window, so a 4k local model works rather than failing every request (DR-015). One generation per round, never two (DR-014).
- **Bounded by design.** Tool output, the context sent to the model, per-agent-call memory, and conversation history are all explicitly bounded, with fail-closed behaviour on overflow rather than silent truncation or unbounded growth. An oversized tool result is preserved in full on disk and previewed head *and* tail, so the failure summary at the end of a build log survives (DR-011).
- **You can see what it changed, and undo it.** Every file AutoSE writes is journalled with the content either side of the change, so a turn ends with a summary (`3 files, +412 −180`), `/diff` shows the unified diff, and `/undo` puts the workspace back. Undo *verifies before it writes*: a file you edited yourself since is reported and left alone rather than overwritten, because an undo that can destroy work it did not create is not one anyone should trust. It works on an uncommitted tree, and on a workspace that is not a repository at all (DR-024).
- **Nothing side-effecting runs unreviewed.** Shell commands and network fetches are shown to you with their actual arguments and wait for a decision: run it once, skip it, or allow that tool for the session — or type a reason, which refuses the call *and* tells the model what to do instead. The gate lives in the shared layer every interface passes through, so it cannot be skipped by a new one, and with no approval interface attached the default is refusal (DR-012).
- **Sessions are durable.** Every turn is written to `.autose/sessions/`, so `autose --resume` picks up an hour of `craft` work after a closed terminal — with its history, its ratcheted tier, its token tally, and the ability to still undo what it wrote (DR-026).
- **git without the interruption.** `git_status`, `git_diff` and `git_log` are ordinary read-only tools, so the most common question an agent has about your repository no longer costs an approval prompt. Fixed argv, no shell, no revisions accepted, paths after a `--` separator (DR-025).
- **Tiered complexity.** One cheap classification call routes each prompt to `spark` (a single direct pass), `craft` (Plan → Execute → Validate) or `forge` (a read-only Design stage in front of those, for architecture-level work) — so a one-line fix does not pay for machinery a real change needs. Within a session automatic classification only ratchets up, so a "yes, do that" inside a complex task does not silently drop the pipeline (DR-003); a tier you pick yourself is used as picked, up or down (DR-049).
- **A completion signal you can actually check.** Multi-stage work runs through a deterministic, IDed requirements ledger. Every stage must echo back exactly the IDs it was given, and the runner re-parses the verifying stage's ledger itself — a claimed pass with anything unverified is overridden into a loopback naming the specific unmet requirements (DR-005). The interface shows you when that happens.
- **Least-privilege stages.** The validation stage has no write or edit tool *in its schema at all*, so it reports gaps and routes them back rather than papering over its own findings. The planning stage has no `write_file`/`edit_file` in its schema either — it cannot edit files — though it can run an approved shell command, the same as validation. (DR-008).
- **Loops you end, not loops that time out.** Nothing caps a run by default: a small local model reads in many more, smaller steps than a hosted one, and a round or retry ceiling sized for a frontier model cuts off correct work two reads from finishing. What ends a long run is you — ctrl-c in the terminal, Stop in the dashboard, SIGINT for a piped run — and stopping keeps what the run produced: the draft, each finished stage's artifacts, the changed-file list, and the undo. Every cap is still there and still per tier, at `-1`: put a number back in one config line for an unattended run where nobody is watching (DR-030). Hitting a bound you *did* set is a distinct, typed outcome that is never rendered as a completed task, and neither is a run you stopped (DR-009).

## Interfaces

- **TUI** — an interactive terminal interface. Streams answers, shows what the model is thinking in a bounded block you can resize with **ctrl-o** while it streams (or re-read afterwards with `/thinking`), reports which tools it is running, and asks before anything touches your shell or the network.
- **JSON** — `autose --json "<prompt>"` runs one prompt with no interactive interface and writes newline-delimited JSON to stdout: an opening line, every event as it happens, and one terminal `result`. Exits `0` when the turn completed and non-zero when it did not, so a script branches on the exit code rather than parsing prose. It is also the executable test of the claim below — a second adapter over the same facade is the only way to find behaviour that had quietly settled in the first one (DR-027).
- **Web dashboard** — `autose --web` (or `python -m gui`) serves a local page (loopback, plus a per-run token) that drives the same session: the request composer, the stage pipeline and activity feed as it works, its reasoning as a separate stream, the approval prompt with the actual command in front of you, the diff and undo, the model and tier pickers, and the stored sessions to reopen. **Several sessions can be open at once and each can be generating independently** — start a long `craft` run, open a second session, and ask something else while the first works; the rail shows what is running where, and a session waiting on an approval says so without seizing the window. A second prompt into a session that is already working is still refused, because one `Session` is not concurrent; `undo` waits for every run to finish, because the workspace is shared and each turn journals its own writes (DR-031). It adds no dependency and has no build step — a standard-library HTTP server, one page, and Server-Sent Events. The page itself fetches nothing from the network (DR-029).
- **Schedules and watchers** — the dashboard can start work while you are away: a *schedule* runs a prompt every *n* minutes or on a cron expression, and a *watcher* runs a command you wrote (say `gh run list --limit 1 --json status,conclusion`) and starts a prompt when its output changes or it fails. Each run opens its own session; shell and network calls in it are refused rather than left waiting for someone who is not there, unless the dashboard was started with `--yes`. `autose --web --service` keeps one token across restarts and opens no browser, and `autose --install-service` writes a systemd user unit for it without starting anything (DR-047).

All three are thin adapters over a single shared `src/logic/` surface — none contains business logic, and `src/logic/` never depends on any of them (see DR-002).

## Decision Records

Every notable decision behind this project — architecture, product scope, non-trivial implementation choices — is recorded in [`docs/DecisionRecords/`](docs/DecisionRecords/) as a numbered DR. Read these before contributing; they are binding context for humans and coding agents alike, not historical trivia (DR-001).

| DR | Decision |
|---|---|
| [DR-001](docs/DecisionRecords/DR-001-maintain-decision-records.md) | Maintain Decision Records for notable changes |
| [DR-002](docs/DecisionRecords/DR-002-src-layout-logic-gui-tui-separation.md) | `src/` layout — logic owns computation, GUI/TUI are thin wrappers |
| [DR-003](docs/DecisionRecords/DR-003-tiered-complexity-classification-and-ratcheting.md) | Complexity tiers (`spark`/`craft`/`forge`) and session ratcheting |
| [DR-004](docs/DecisionRecords/DR-004-bounded-memory-architecture.md) | Bounded memory architecture — execution-memory scopes (knowledge stores are Pro, DR-032) |
| [DR-005](docs/DecisionRecords/DR-005-canonical-requirements-ledger-fail-closed-validation.md) | Canonical requirements ledger and fail-closed validation |
| [DR-006](docs/DecisionRecords/DR-006-provider-agnostic-byo-llm-inference.md) | Provider-agnostic, bring-your-own-LLM inference |
| [DR-007](docs/DecisionRecords/DR-007-validated-batch-single-file-edits.md) | Validated, batch single-file edit tool |
| [DR-008](docs/DecisionRecords/DR-008-stage-isolated-least-privilege-tool-registries.md) | Stage-isolated, least-privilege tool registries |
| [DR-009](docs/DecisionRecords/DR-009-bounded-agent-loops-time-and-iteration-caps.md) | Bound agent loops with time budgets and iteration caps |
| [DR-010](docs/DecisionRecords/DR-010-calibrated-context-token-budget-safety-margin.md) | Calibrate local context estimates with a safety margin |
| [DR-011](docs/DecisionRecords/DR-011-recoverable-bounded-tool-output.md) | Preserve oversized tool output as recoverable artifacts |
| [DR-012](docs/DecisionRecords/DR-012-approval-gated-side-effecting-tools.md) | Approval-gated execution for side-effecting and network tools |
| [DR-013](docs/DecisionRecords/DR-013-pro-public-edition-gui-differentiator.md) | Pro vs. public edition — GUI is the only current differentiator (superseded by DR-032) |
| [DR-014](docs/DecisionRecords/DR-014-single-generation-streaming-rounds.md) | Streaming rounds carry their own tool calls (one generation per round) |
| [DR-015](docs/DecisionRecords/DR-015-local-first-inference-defaults.md) | Local inference is the default target; hosted APIs are optional |
| [DR-016](docs/DecisionRecords/DR-016-reasoning-output-is-a-separate-channel.md) | A reasoning model's chain of thought is a separate, non-replayed channel |
| [DR-017](docs/DecisionRecords/DR-017-ledger-tolerance-and-derived-verdict.md) | The ledger tolerates presentation variance; the verdict is derived, not asked for |
| [DR-019](docs/DecisionRecords/DR-019-nudge-a-silent-turn-instead-of-restarting-the-stage.md) | Nudge a silent turn rather than restarting the stage |
| [DR-020](docs/DecisionRecords/DR-020-request-minimal-reasoning-effort-for-classification.md) | Request minimal reasoning effort for the classification call |
| [DR-021](docs/DecisionRecords/DR-021-tuning-and-prompts-in-workspace-config.md) | Tuning knobs and prompts live in one repo-local `.autose/config.yaml` |
| [DR-022](docs/DecisionRecords/DR-022-budget-against-the-served-context-window.md) | Budget against the window the runtime actually serves |
| [DR-023](docs/DecisionRecords/DR-023-revalidate-every-redirect-hop.md) | Re-validate the address check on every redirect hop |
| [DR-024](docs/DecisionRecords/DR-024-journalled-reviewable-revertible-file-changes.md) | File changes are journalled, reviewable, and revertible — not gated by default |
| [DR-025](docs/DecisionRecords/DR-025-read-only-git-tools.md) | Read-only git inspection is a built-in tool, not an approved shell command |
| [DR-026](docs/DecisionRecords/DR-026-durable-resumable-sessions.md) | A session is durable and resumable, one file per session |
| [DR-027](docs/DecisionRecords/DR-027-non-interactive-json-adapter.md) | A non-interactive JSON adapter, and what it proves about DR-002 |
| [DR-029](docs/DecisionRecords/DR-029-the-gui-adapter-is-a-local-web-dashboard.md) | The GUI adapter is a local web dashboard, served by the standard library |
| [DR-030](docs/DecisionRecords/DR-030-unlimited-by-default-caps-and-a-user-held-stop.md) | Every cap is unlimited by default; the user holds the stop |
| [DR-031](docs/DecisionRecords/DR-031-concurrent-sessions-in-the-dashboard.md) | The dashboard holds several sessions at once, each with its own turn |
| [DR-032](docs/DecisionRecords/DR-032-pro-edition-is-the-decision-record-and-knowledge-layer.md) | Pro is the decision-record and knowledge layer; the dashboard ships in both editions |
| [DR-033](docs/DecisionRecords/DR-033-the-live-view-survives-a-reload.md) | The dashboard's live view survives a reload, and shows usage as it is spent |
| [DR-034](docs/DecisionRecords/DR-034-context-advice-not-a-fixed-warning.md) | A served context window gets a verdict and a computed remedy, not a fixed warning |
| [DR-035](docs/DecisionRecords/DR-035-autose-chooses-the-context-window.md) | AutoSE chooses the context window where the runtime allows it, and the user can change it |
| [DR-036](docs/DecisionRecords/DR-036-provider-settings-keys-and-per-session-switching.md) | Providers are chosen per session, keys live in an owner-only credentials file, and Ollama Cloud is a provider |
| [DR-037](docs/DecisionRecords/DR-037-the-inspector-shows-the-run-in-full.md) | The dashboard has an inspector that shows the run in full; the conversation stays compact |
| [DR-038](docs/DecisionRecords/DR-038-the-requirements-ledger-is-kept-and-shown.md) | Each turn keeps its final requirements ledger, and the dashboard shows it as a board |
| [DR-039](docs/DecisionRecords/DR-039-a-malformed-tool-call-is-retried.md) | A tool call the runtime cannot parse is retried, not fatal |
| [DR-040](docs/DecisionRecords/DR-040-the-changes-tab-reads-any-turn-s-diff.md) | The inspector's Changes tab lists every turn's writes and reads any turn's diff on demand |
| [DR-041](docs/DecisionRecords/DR-041-the-usage-tab-shows-where-tokens-and-time-went.md) | The inspector's Usage tab shows where the tokens and the time went |
| [DR-042](docs/DecisionRecords/DR-042-session-tabs-and-a-queue-per-session.md) | Open sessions are tabs, and each session queues prompts sent while it works |
| [DR-043](docs/DecisionRecords/DR-043-desktop-notifications-for-what-waits.md) | The dashboard raises desktop notifications for what waits on the person |
| [DR-044](docs/DecisionRecords/DR-044-approvals-inline-with-a-needs-you-inbox.md) | Approvals are answered inline, listed under "Needs you", and can be granted for the session |
| [DR-045](docs/DecisionRecords/DR-045-a-person-can-review-the-plan-before-execute.md) | A person can review the plan before anything is changed |
| [DR-047](docs/DecisionRecords/DR-047-schedules-watchers-and-the-dashboard-as-a-local-service.md) | Schedules and watchers start unattended runs, whose approvals are refused at once; the dashboard can run as a local service |
| [DR-048](docs/DecisionRecords/DR-048-session-allowances-cover-calls-like-this-one.md) | "Allow for this session" covers calls like this one, not the whole tool |
| [DR-049](docs/DecisionRecords/DR-049-a-tier-a-person-chooses-is-used-as-chosen.md) | A tier a person chooses is used as chosen; the ratchet is for classification |
| [DR-050](docs/DecisionRecords/DR-050-output-shaping-and-rules-written-by-hand.md) | Output shaping does not block a standing yes, and allow rules can be written by hand |
| [DR-051](docs/DecisionRecords/DR-051-a-read-only-files-view.md) | A read-only Files view in the dashboard |
| [DR-052](docs/DecisionRecords/DR-052-chains-are-allowed-part-by-part.md) | A chain of commands is allowed part by part |
| [DR-053](docs/DecisionRecords/DR-053-yolo-mode.md) | YOLO mode: a switch that lets everything run without asking |
| [DR-054](docs/DecisionRecords/DR-054-forge-ships-in-the-free-edition.md) | `forge` ships in the free edition, without the decision record |
| [DR-055](docs/DecisionRecords/DR-055-a-run-s-steps-are-kept-with-it.md) | A run's steps and usage are kept with it |
| [DR-056](docs/DecisionRecords/DR-056-a-session-s-queue-is-saved-with-it.md) | A session's queue is saved with it and comes back held |
| [DR-057](docs/DecisionRecords/DR-057-allow-rules-that-last.md) | Allow rules that last: saved with the session, and for a workspace |
| [DR-058](docs/DecisionRecords/DR-058-a-session-keeps-its-provider-and-follows-its-key.md) | A reopened session goes back to its provider, and open sessions follow a rotated key |
| [DR-059](docs/DecisionRecords/DR-059-match-the-loaded-window-and-save-a-workspace-default.md) | A window nobody chose matches the loaded model, and a chosen one can be saved for the workspace |
| [DR-060](docs/DecisionRecords/DR-060-a-command-menu.md) | A command menu in the dashboard |

DR-018 and DR-028 describe Pro-only features and are kept in the Pro repository.

## Layout

```text
src/
  logic/              # all computation — nothing here imports an interface
    common/           #   agent loop, tools, approval, context budget, memory,
                      #   and the change journal behind /diff and /undo
    inference/        #   provider-agnostic chat models, discovery, catalogs
    workflows/        #   one interchangeable workflow per tier, and its stages
    session.py        #   the public entry point every interface drives
    classifier.py     #   one call picks a tier, with a deterministic fallback
    requirements.py   #   the deterministic ledger parser (DR-005)
    tiers.py          #   single source of truth for spark/craft/forge
    diagnostics.py    #   "what can this machine run" as data, not output
  tui/                # terminal and JSON adapters — rendering and input only, DR-002
  gui/                # web dashboard — DR-002/DR-029
    bridge.py         #   one session, driven over HTTP: frames, approvals, locks
    server.py         #   stdlib HTTP + SSE, loopback, token, Host/Origin checks
    static/           #   the page, as shipped — no build step, no remote assets
docs/
  DecisionRecords/    # DR-NNN-*.md — binding project decisions, see DR-001
tests/                # includes executable checks for the DR-002 boundaries
```

The boundary is enforced, not just documented: `tests/test_architecture.py` fails if `logic/` imports an interface, if orchestration reappears as a sibling of `logic/`, if a retired tier name returns, or if anything in `logic/` or `tui/` would stop `gui/` from being a removable adapter.

## Known limits

Worth naming rather than discovering:

- **Undo reverts the most recent turn that wrote**, not an arbitrary point in the session.
- **A stop lands at the next safe point, not instantly.** Stopping is cooperative (DR-030): the run unwinds between rounds, tool calls or stages, so a stop asked for mid-round waits for that round's inference to return. A second ctrl-c aborts outright, at the cost of the partial outcome. The one place a stop cannot reach at all is a `run_command` that has not returned — hence the shell timeout is the one cap still bounded by default.

## Configuration

None is required. To override the defaults, set environment variables (`AUTOSE_BASE_URL`, `AUTOSE_MODEL`, `AUTOSE_API_KEY`, `AUTOSE_PROVIDER`, `AUTOSE_CONTEXT_LIMIT`, `AUTOSE_WORKSPACE`) or write `~/.config/autose/config.yaml`. Environment wins over the file, and the file wins over discovery.

API keys for hosted providers (Ollama Cloud, an OpenAI-compatible service) are best saved from the dashboard's settings, which keep them in an owner-only `~/.config/autose/credentials.yaml` rather than in `config.yaml`, and never send them back to the browser. `OLLAMA_API_KEY` also works for Ollama Cloud. The dashboard can switch any one session to any configured provider, and save the provider and model new sessions start with (DR-036).

Run `/config` inside AutoSE to drop a commented `.autose/config.yaml` into the current workspace. Every setting is written at the value already in effect, so editing one changes behaviour and deleting a key restores the built-in default — including the prompt each stage is sent (DR-021).

Runtime configuration covers only the endpoint, the workspace, and the approval posture.

### Getting a usable context window out of Ollama

Ollama serves every model in a 4096-token window unless you tell it otherwise, whatever the model itself supports. That is a property of the runtime, not of AutoSE, and nothing in an OpenAI-compatible request can raise it. AutoSE will warn you when it detects the gap; to close it, restart Ollama with a larger window:

```bash
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
```

Pick a size your hardware can actually hold — a larger window costs VRAM, and a model that no longer fits will be split onto the CPU and slow down sharply. `autose --doctor` reports what the runtime is currently serving (DR-022).

## Development

```bash
git clone https://github.com/AutoSE-Labs/autose.git && cd autose
pip install -e ".[dev]"
ruff check .      # lint
mypy              # types — src/ is fully annotated
pytest -q         # the suite, ~800 tests, a few seconds
```

CI runs all three on Python 3.11–3.13 (plus macOS and Windows on 3.13), and separately checks that every decision record is numbered uniquely and linked from this file. No inference runtime is needed: every test drives the scripted `ChatModel` double from `tests/support/fakes.py`, which is what DR-006's provider seam is for.

## Contributing

Any notable decision — a new feature, an architecture or module-boundary choice, a non-trivial implementation choice where alternatives existed — gets a DR alongside the change that introduces it, per DR-001. If a change would contradict an `Accepted` DR, it must either conform to that DR or explicitly supersede it.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

This repository is the free edition. Per DR-032 and DR-054 it is the Pro source with the knowledge layer and the Design stage's decision record removed, and nothing else changed.
