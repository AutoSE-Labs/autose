# Energy monitoring

How AutoSE reports energy for **local Ollama** inference in the CLI and Tauri app.

**Product rules**

- User-level only — no sudo, no privileged helper, no `powermetrics` in the product path
- Fail-open — inference never depends on energy
- Numbers are **inference-window** estimates (not idle-subtracted whole-machine energy)
- Always show provenance (`measured` / `estimated` / `approximated` / `unavailable`)

## What we do

On every successful local chat/completion call (`tracking.py` wraps the agent `_call_sync`):

1. Cache Ollama `/api/show` + `/api/tags` model meta (size / params / quant)
2. Open a short energy span around the HTTP call
3. Prefer a **user-level hardware sensor** if one works
4. Else **approximate warm inference** from timings + device-family profile + model size
5. Emit `energy_updated` (headless/desktop) and accumulate session totals for the CLI status bar / Tauri footer

Failed calls do not add energy. Approximation targets the **already-loaded** case (model resident in memory). First-load / cold-start cost is out of scope.

## How it chooses a path

1. **Measure** if a normal-user sensor works
2. Else **approximate** from timings + device-family profile + model meta
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

Calibration **v4** (warm inference only):

\[
E \approx P_{\mathrm{phase}}\, t_{\mathrm{phase}} + c_{\mathrm{mem}} \cdot \mathrm{GiB} \cdot T
\]

| Term | Meaning |
| --- | --- |
| \(P\,t\) | Device-family watts × time. Prefer native Ollama `prompt_eval_duration` / `eval_duration` when present; else wall-clock. **Watts are not scaled by model size** — heavier models already take longer |
| \(\mathrm{GiB}\) | Prefer Ollama `/api/tags` blob `size`; else params × quant bits |
| \(T\) | Token-weighted traffic (decode-heavy; prefill discounted) |

No cold-load term: we report steady use after the model is already loaded.

Device identity (unprivileged): NVIDIA GPU name, or Apple `hw.model` / chip string → broad family bucket (Apple M-series / NVIDIA class / generic).

Bounds widen when size is unknown, phase timings are missing, or the call is short. Each result includes `confidence`, `calibration_id`, and lower/upper bounds.

## Display

Always show provenance, e.g.:

- `12.4 J · measured · GPU (nvidia-smi)`
- `3.1 J · measured · CPU package (RAPL)`
- `~90 J (45–140) · approximated · Apple M4 · low`

## Where it lives

| Piece | Role |
| --- | --- |
| `code/energy/monitor.py` | In-process orchestration |
| `code/energy/collectors.py` | NVIDIA / RAPL sensors |
| `code/energy/approximation.py` | Timing + size-based estimate |
| `code/energy/calibration.py` | Device-family watt / load / memory profiles (v4) |
| `code/energy/ollama_meta.py` | `/api/show`, `/api/tags` |
| `code/energy/platform_probe.py` | Unprivileged device identity |
| `code/energy/tracking.py` | Wraps CLI/desktop agent `_call_sync` |
| CLI TUI status bar | Session energy (`code/tui/display.py`) |
| Tauri footer + receipt | Session + per-call via `energy_updated` events |

CLI and Tauri share the Python agent path; energy is emitted as headless `energy_updated` events and included in session `usage`.
