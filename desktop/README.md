# AutoSE Desktop

Tauri desktop shell for the AutoSE headless core.

The desktop app keeps the agent runtime in Python. Rust is only used for the Tauri command bridge that launches:

```sh
uv run autose --events --mode <mode> --workspace <path> "<task>"
```

## Quick Start

Prerequisites:

- Node.js
- Rust toolchain
- Tauri platform dependencies
- `uv` available on `PATH`

From this directory:

```sh
npm install
npm run doctor
npm run dev
```

Use the app:

1. Enter a task in the task box.
2. Set the workspace path to the project AutoSE should inspect or modify.
3. Choose `auto`, `lite`, or `standard`.
4. Leave **Approve commands** off unless you want shell commands approved automatically.
5. Click **Run** and watch the live timeline, result, and artifacts.

The first screen supports task input, mode selection, workspace selection, optional command auto-approval, streamed timeline events, final result rendering, and artifacts.

On Ubuntu, the native Tauri build requires Rust and the WebKit/GTK development packages. If `npm run doctor` reports missing native libraries, install:

```sh
sudo apt install pkg-config libdbus-1-dev libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev libssl-dev
```
