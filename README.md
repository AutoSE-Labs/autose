# AutoSE

An AI-powered software engineering agent with an interactive TUI.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) **or** pip
- An OpenAI-compatible LLM inference endpoint

## Installation

### Using uv (recommended)

```bash
cd autose
uv sync
```

This installs the package and its dependencies into a managed virtual environment.

### Using pip

```bash
cd autose
pip install -e .
```

## Configuration

Copy and edit the provided config file:

```bash
cp profiles/config.yaml profiles/config.yaml   # already present — just edit it
```

`profiles/config.yaml`:

```yaml
inference:
  provider: openai
  base_url: http://your-llm-server/v1   # OpenAI-compatible endpoint
  api_key: ""                            # leave empty if not required
  model: your-model-name
  context_limit: 16384
```

Point `base_url` at any OpenAI-compatible server (Ollama, vLLM, LM Studio, OpenAI, etc.).

## Usage

### With uv

```bash
# Interactive TUI
uv run autose

# Pass an initial prompt directly
uv run autose "refactor the authentication module to use JWT"
```

### After pip install (console script)

```bash
autose
autose "write unit tests for utils.py"
```

### Run directly from the repo root

```bash
uv run python autose.py
uv run python autose.py "your task here"
```

## Desktop App

AutoSE also includes an early Tauri desktop shell in `desktop/`. The desktop app uses the same Python agent core as the CLI, but shows task progress, streamed events, final summaries, and artifacts in a graphical interface.

```bash
cd desktop
npm install
npm run doctor
npm run dev
```

If `npm run doctor` reports missing native packages, install the listed Tauri/WebKit dependencies and rerun it. In the app, enter a task, choose a workspace folder path, select a mode (`auto`, `lite`, or `standard`), then click **Run**.

## How It Works

1. You enter a prompt in the TUI.
2. The **classifier** determines the complexity tier: `lite` or `standard`.
3. The request is dispatched to the matching agent pipeline.
4. Results are streamed back in the TUI. Complexity only ever increases within a session — a lite-classified follow-up in an active standard session still uses the full pipeline.

## Benchmarks

### Terminal-Bench 2.1

AutoSE is evaluated on [Terminal-Bench 2.1](https://www.tbench.ai/benchmarks/terminal-bench-2-1) — a benchmark of ~89 real-world terminal tasks spanning system administration, software engineering, security, data science, and machine learning.

The evaluation uses AutoSE's **Standard pipeline** (Plan → Code → Test) running as a [Harbor](https://github.com/harbor-framework/harbor) external agent. Each task runs in an isolated Docker container; AutoSE explores the environment, implements a solution, and self-verifies with tests — all driven by the configured LLM endpoint.

#### Running the benchmark

```bash
# Single task at a time (safe first run)
uv run benchmarks/tbench.py

# Parallel tasks
uv run benchmarks/tbench.py --n-concurrent 4

# One specific task
uv run benchmarks/tbench.py --task crack-7z-hash

# Different dataset version
uv run benchmarks/tbench.py --dataset terminal-bench@2.0
```

Results are saved to `benchmarks/results/tbench/<run-id>/`. Each trial writes a detailed `autose.log` with the full Plan → Code → Test trace.

#### Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) running locally
- `harbor` CLI — installed automatically by `tbench.py` if absent (`uv tool install harbor`)
