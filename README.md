# AutoSE

An AI-powered software engineering agent with an interactive TUI and a desktop app.

You bring your own LLM: point AutoSE at any OpenAI-compatible inference endpoint (Ollama, vLLM, LM Studio, OpenAI, etc.). AutoSE never provides or proxies models itself.

## Desktop App (recommended for most users)

Download **AutoSE-Setup.exe** from the [latest release](https://github.com/AutoSE-Labs/autose/releases/latest) and run it. No other software is required:

1. The installer sets up the app for the current user (no admin prompt).
2. On first launch, the app automatically installs its Python runtime (via [uv](https://docs.astral.sh/uv/)) — this happens once and needs an internet connection.
3. Open **Settings** (gear icon) and enter your inference endpoint: base URL, optional API key, and model name.

Settings are stored in `%APPDATA%\AutoSE\config.yaml`; the managed runtime lives in `%LOCALAPPDATA%\AutoSE`.

## Terminal Version

### Requirements

- [uv](https://docs.astral.sh/uv/) (recommended) **or** Python 3.13+ with pip
- An OpenAI-compatible LLM inference endpoint

### Install and run

```bash
git clone https://github.com/AutoSE-Labs/autose.git
cd autose
uv sync
uv run autose
```

Or with pip: `pip install -e .` then run `autose`.

```bash
# Interactive TUI
uv run autose

# Pass an initial prompt directly
uv run autose "refactor the authentication module to use JWT"
```

## Configuration

AutoSE looks for its config file in this order:

1. The path in the `AUTOSE_CONFIG` environment variable
2. `%APPDATA%\AutoSE\config.yaml` (Windows) or `~/.config/autose/config.yaml` (macOS/Linux) — this is where the desktop app's Settings screen writes
3. `profiles/config.yaml` in the repo (terminal/git-clone installs)

```yaml
inference:
  provider: openai
  base_url: http://your-llm-server/v1   # OpenAI-compatible endpoint
  api_key: ""                            # leave empty if not required
  model: your-model-name
  context_limit: 262144
```

## How It Works

1. You enter a prompt in the TUI or desktop app.
2. The **classifier** determines the complexity tier: `lite` or `standard`.
3. The request is dispatched to the matching agent pipeline.
4. Results are streamed back. Complexity only ever increases within a session — a lite-classified follow-up in an active standard session still uses the full pipeline.

## Developing / Building from Source

End users never need this — the installer above is prebuilt. Building the desktop app from source requires Node.js 20+, the Rust toolchain, and uv:

```bash
cd desktop
npm install
npm run dev      # develop against the repo checkout
npm run build    # produces the NSIS installer under src-tauri/target/release/bundle/nsis/
```

`npm run build` first stages the Python backend into `src-tauri/backend/` (see `desktop/scripts/stage-backend.mjs`) so it ships inside the installer.

### Releasing

Push a `v*` tag (matching the `version` in `desktop/src-tauri/tauri.conf.json`) and the `Release` GitHub Actions workflow builds the Windows installer and attaches it to a GitHub Release, including the stable `AutoSE-Setup.exe` alias.
