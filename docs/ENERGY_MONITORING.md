# Energy monitoring

How AutoSE reports energy for **local Ollama** inference in the CLI and Tauri app.

**Product rules**

- User-level only — no sudo, no privileged helper, no `powermetrics` in the product path
- Fail-open — inference never depends on energy
- Numbers are **inference-window** estimates (not idle-subtracted whole-machine energy)
- Always show provenance (`measured` / `estimated` / `approximated` / `unavailable`)

## What we do

On every successful local chat/completion call (`tracking.py` wraps the agent `_call_sync`):

1. Open a short energy span around the HTTP call
2. Prefer a **user-level hardware sensor** if one works
3. Else **approximate** from timings + device profile + Ollama model metadata
4. Emit `energy_updated` (headless/desktop) and accumulate session totals for the CLI status bar / Tauri footer

Failed calls do not add energy.

## How it chooses a path

1. **Measure** if a normal-user sensor works
2. Else **approximate** from Ollama timings + device profile + model meta
3. Else **unavailable**

## Devices

| Device | How | What you get | Label |
| --- | --- | --- | --- |
| **Linux + NVIDIA** | `nvidia-smi power.draw` while the call runs | GPU board joules | `measured` · scope `gpu` |
| **Windows + NVIDIA** | `nvidia-smi.exe power.draw` | GPU board joules | `measured` · scope `gpu` |
| **Linux + Intel RAPL** (sysfs readable) | `/sys/class/powercap/.../energy_uj` | CPU package joules | `measured` · scope `cpu_package` |
| **macOS (Apple Silicon)** | No user-level SoC power API → approximate | Modeled SoC energy | `approximated` · scope `modeled_active_device` |
| **Windows non-NVIDIA** | Approximate | Modeled device energy | `approximated` |
| **Linux non-NVIDIA, no RAPL** | Approximate | Modeled device energy | `approximated` |
| **Unknown / no signals** | — | null joules | `unavailable` |

Concurrent measured calls that share one sensor interval split energy and are labelled `estimated` (attribution). That is **not** the same as model approximation.

## Approximation (macOS and other fallbacks)

Calibration **v3** hybrid estimate:

\[
E \approx (W_{\mathrm{prefill}} \cdot s)\, t_{\mathrm{prefill}} + (W_{\mathrm{decode}} \cdot s)\, t_{\mathrm{decode}} + c_{\mathrm{mem}} \cdot \mathrm{GiB} \cdot T
\]

| Symbol | Meaning |
| --- | --- |
| \(t\) | Ollama `prompt_eval_duration` / `eval_duration` when available; else wall-clock (OpenAI-compat `/v1`) |
| \(W\) | Bundled device profile watts (Apple M-series / NVIDIA class / generic) |
| \(s\) | **Model load scale** from Ollama `/api/show` (params, quant bits, MoE active vs total). Soft power-law vs a ~7B Q4 reference so same latency ≠ same energy for 5B vs 70B |
| \(\mathrm{GiB}\) | Approx active weight footprint from params × bits |
| \(T\) | Token-weighted traffic (decode-heavy; prefill discounted) |

Device identity (unprivileged): NVIDIA GPU name, or Apple `hw.model` / chip string (e.g. `Mac16,12`, `Apple M4`).

Missing metadata → lower confidence + wider bounds. Each approximated result includes `confidence`, `calibration_id`, and lower/upper bounds.

Apple M4 profile watts were seeded from a local `powermetrics` spot-check (MacBook Air M4 / `Mac16,12`). That tool is **validation-only** — not shipped in AutoSE.

### Why not watts × time alone?

Latency already reflects some load, but two models can finish in similar time with very different SoC draw and memory traffic. Scaling \(W\) by model intensity and adding a memory×tokens term keeps larger / higher-precision models above smaller ones when wall time is comparable.

## Display

Always show provenance, e.g.:

- `12.4 J · measured · GPU (nvidia-smi)`
- `3.1 J · measured · CPU package (RAPL)`
- `~41 J (25–58) · approximated · Apple M4 · medium`

## Where it lives

| Piece | Role |
| --- | --- |
| `code/energy/monitor.py` | In-process orchestration |
| `code/energy/collectors.py` | NVIDIA / RAPL sensors |
| `code/energy/approximation.py` | Timing + model-intensity estimate |
| `code/energy/calibration.py` | Device watt / memory profiles (v3) |
| `code/energy/ollama_meta.py` | Ollama `/api/show` metadata |
| `code/energy/platform_probe.py` | Unprivileged device identity |
| `code/energy/tracking.py` | Wraps CLI/desktop agent `_call_sync` |
| CLI TUI status bar | Session energy (`code/tui/display.py`) |
| Tauri footer + receipt | Session + per-call via `energy_updated` events |

CLI and Tauri share the Python agent path; energy is emitted as headless `energy_updated` events and included in session `usage`.
