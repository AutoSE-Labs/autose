# ADR-002: Tauri Desktop Shell

- Status: Accepted
- Date: 2026-07-06
- Deciders: AutoSE maintainers
- Technical Story: Start the AutoSE desktop application while keeping agent logic in the shared Python core.

## Context

ADR-001 established that AutoSE should use a shared core with first-class CLI and desktop clients. The desktop client now needs an implementation shell.

The main product requirement is a Codex-like task experience: users should submit a task, see progress, respond to approvals, and receive summaries and artifacts. The desktop app should not become a second implementation of the agent runtime.

The team considered native webview-style shells, PyWebView, Electron, and Tauri. Tauri was selected for the initial desktop direction despite the added Rust/tooling requirement.

## Decision

AutoSE will start the desktop app with Tauri.

The Tauri application will be a thin shell:

- Frontend UI is implemented with web technology.
- Rust is limited to desktop integration and command bridging.
- Agent orchestration remains in the Python shared core.
- The desktop shell invokes the headless CLI/core boundary and renders structured session output.

Rust must not become the location for agent planning, coding, test orchestration, tool execution, or model-provider behavior.

## Consequences

Positive:

- Keeps desktop packaging and native integration on a serious path.
- Preserves the shared-core architecture from ADR-001.
- Avoids duplicating Python agent logic in the desktop layer.
- Allows the first desktop screen to be built around task, progress, summary, and artifacts.

Negative:

- Contributors now need Node.js, Rust, and Tauri platform dependencies for desktop work.
- The first prototype depends on invoking `uv run autose` from the Tauri bridge.
- Production packaging still needs a sidecar or bundled runtime strategy.

## Implementation Notes

The initial desktop scaffold lives under `desktop/`.

The bridge command should execute:

```sh
uv run autose --json --mode <mode> --workspace <path> "<task>"
```

The CLI now accepts `--workspace` so desktop runs can target a project root while preserving the previous default behavior.

Future work should replace the blocking JSON call with an event-streaming bridge once the session event contract is stable enough for live rendering.
