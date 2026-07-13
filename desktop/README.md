# AutoSE Desktop

Tauri desktop shell for the AutoSE headless core.

The agent runtime stays in Python. Rust is only the Tauri command bridge that launches:

```sh
uv run autose --events --mode <mode> --workspace <path> "<task>"
```

## Installing (end users)

Download **AutoSE-Setup.exe** from the [latest release](https://github.com/AutoSE-Labs/autose/releases/latest) and run it — no Rust, Node, Python, or uv required. On first launch the app installs its own Python runtime (one-time, needs internet), then asks for your OpenAI-compatible inference endpoint in Settings.

- Config: `%APPDATA%\AutoSE\config.yaml`
- Managed runtime (backend copy, venv, bundled uv): `%LOCALAPPDATA%\AutoSE`

## Developing (from source)

Prerequisites: Node.js 20+, Rust toolchain, Tauri platform dependencies, `uv` on `PATH`.

From this directory:

```sh
npm install
npm run doctor
npm run dev
```

Dev builds (`npm run dev`) run the agent straight from the repo checkout via `uv run autose`; no bootstrap is needed. The setup screen only appears in installed release builds.

## Building the installer

```sh
npm run build
```

This stages the Python backend into `src-tauri/backend/` (`scripts/stage-backend.mjs`), builds the frontend, compiles the Rust shell, and produces the NSIS installer under `src-tauri/target/release/bundle/nsis/`.

## How the installed app bootstraps

1. `backend_status` reports `needs_setup` when the staged version marker, venv, or uv is missing.
2. `bootstrap_backend` copies the bundled backend to `%LOCALAPPDATA%\AutoSE\backend`, installs uv to `%LOCALAPPDATA%\AutoSE\uv` if not found (official installer, `UV_NO_MODIFY_PATH=1`), runs `uv sync --frozen` (uv provisions Python 3.13), and seeds the default config.
3. Every agent run sets `AUTOSE_CONFIG` to the user config so the Python core reads the endpoint saved from the Settings screen.

On Ubuntu, the native Tauri build requires Rust and the WebKit/GTK development packages. If `npm run doctor` reports missing native libraries, install:

```sh
sudo apt install pkg-config libdbus-1-dev libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev libssl-dev
```
