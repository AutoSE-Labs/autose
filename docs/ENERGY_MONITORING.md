# Energy monitoring

Brief guide to how AutoSE reports energy for local Ollama inference.

**Rules:** user-level only (no sudo / privileged helper). Fail-open — inference never depends on energy. Numbers are inference-window estimates, not idle-subtracted whole-machine energy.

## How it chooses a path

1. **Measure** if a normal-user sensor works  
2. Else **approximate** from Ollama timings + device profile  
3. Else **unavailable**

## Devices

| Device | How | What you get | Label |
| --- | --- | --- | --- |
| **Linux + NVIDIA** | `nvidia-smi power.draw` while the call runs | GPU board joules | `measured` · scope `gpu` |
| **Windows + NVIDIA** | `nvidia-smi.exe power.draw` | GPU board joules | `measured` · scope `gpu` |
| **Linux + Intel RAPL** (sysfs readable) | `/sys/class/powercap/.../energy_uj` | CPU package joules | `measured` · scope `cpu_package` |
| **macOS (Apple Silicon)** | No user-level SoC power API → approximate | Modeled SoC energy | `approximated` · scope `modeled_active_device` |
| **Windows non-NVIDIA** | Approximate (no standard live watts API) | Modeled device energy | `approximated` |
| **Linux non-NVIDIA, no RAPL** | Approximate | Modeled device energy | `approximated` |
| **Unknown / no signals** | — | null joules | `unavailable` |

Concurrent measured calls that share one sensor interval split energy and are labelled `estimated` (attribution), which is **not** the same as model approximation.

## Approximation (when not measuring)

\[
E \approx W_{\mathrm{prefill}} \times t_{\mathrm{prefill}} + W_{\mathrm{decode}} \times t_{\mathrm{decode}}
\]

- \(t\) from Ollama `prompt_eval_duration` / `eval_duration` (already reflects model size, quant, MoE)
- \(W\) from bundled device profiles (chip/GPU bucket), **not** scaled by parameter count
- Device identity (unprivileged): NVIDIA GPU name, or Apple `hw.model` / chip string (e.g. `Mac16,12`, `Apple M4`)
- Missing timings → joules-per-token fallback; unknown hardware → `generic_cpu`, low confidence + wide bounds

Each approximated result includes `confidence`, `calibration_id`, and lower/upper bounds.

## Display

Always show provenance, e.g.:

- `12.4 J · measured · GPU (nvidia-smi)`
- `3.1 J · measured · CPU package (RAPL)`
- `~31 J (20–42) · approximated · Apple M4 · medium`

## Where it lives

| Piece | Role |
| --- | --- |
| `code/energy/monitor.py` | In-process orchestration |
| `code/energy/collectors.py` | NVIDIA / RAPL sensors |
| `code/energy/approximation.py` | Timing/model estimate |
| `code/energy/calibration.py` | Device watt profiles |
| `code/energy/tracking.py` | Wraps CLI/desktop agent `_call_sync` |
| CLI TUI status bar | Session energy (`code/tui/display.py`) |
| Tauri footer + receipt | Session + per-call via `energy_updated` events |

CLI and Tauri share the Python agent path; energy is emitted as headless `energy_updated` events and included in session `usage`.
