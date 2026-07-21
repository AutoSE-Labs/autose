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
4. Else **approximate warm inference** from timings + hardware-tier profile + model size
5. Emit `energy_updated` (headless/desktop) and accumulate session totals for the CLI status bar / Tauri footer

Failed calls do not add energy. Approximation targets the **already-loaded** case (model resident in memory). First-load / cold-start cost is out of scope.

## How it chooses a path

1. **Measure** if a normal-user sensor works
2. Else **approximate** from timings + hardware-tier watt table + model meta
3. Else **unavailable**

## Devices

| Device | How | What you get | Label |
| --- | --- | --- | --- |
| **Linux + NVIDIA** | `nvidia-smi power.draw` while the call runs | GPU board joules | `measured` · scope `gpu` |
| **Windows + NVIDIA** | `nvidia-smi.exe power.draw` | GPU board joules | `measured` · scope `gpu` |
| **Linux + Intel RAPL** (sysfs readable) | `/sys/class/powercap/.../energy_uj` | CPU package joules | `measured` · scope `cpu_package` |
| **macOS (Apple Silicon)** | No user-level SoC power API → approximate | Modeled SoC energy | `approximated` · scope `modeled_active_device` |
| **Windows/Linux, no live power** | GPU name (+ optional `power.limit`) → TGP band | Modeled GPU energy | `approximated` |
| **Unknown / no signals** | — | null joules or `generic_cpu` | `unavailable` / low-confidence approx |

Concurrent measured calls that share one sensor interval split energy and are labelled `estimated` (attribution). That is **not** the same as model approximation.

## Approximation (when not measuring)

Calibration **v5** (warm inference only):

\[
E \approx P_{\mathrm{tier}}\, t + c_{\mathrm{mem}} \cdot \mathrm{GiB} \cdot T
\]

| Term | Meaning |
| --- | --- |
| \(P\,t\) | Hardware-tier watts × time. Prefer native phase timings when present; else wall-clock. Watts are **not** scaled by model size |
| \(\mathrm{GiB}\) | Prefer Ollama `/api/tags` blob `size`; else params × quant bits |
| \(T\) | Token-weighted traffic (decode-heavy; prefill discounted) |

### How the tier (active watts) is chosen

Identity is separate from power measurement:

1. **macOS** — `sysctl` / `system_profiler` chip string → Apple tier (`base` / `Pro` / `Max` / `Ultra`)
2. **NVIDIA present** — `nvidia-smi --query-gpu=name,power.limit` even when `power.draw` fails
3. **Windows, no nvidia-smi** — `Win32_VideoController` name via CIM/WMI
4. Map name (and `power.limit` when present) → a **TGP/TDP band** in `calibration.py`

Bands are coarse groups of similar published board power (not one row per SKU). Examples:

| Band | Published anchor |
| --- | --- |
| `nvidia_desktop_450` | RTX 4090 TGP 450 W (NVIDIA); avg gaming ~315 W |
| `nvidia_desktop_320` | RTX 4080 TGP 320 W; RTX 3080 TGP 320 W |
| `nvidia_desktop_200` | RTX 4070 TGP 200 W; RTX 3070 TGP 220 W |
| `nvidia_laptop_140` | RTX 4050/4060/4070 Laptop up to ~115 W + Dynamic Boost |
| `nvidia_laptop_175` | RTX 4080/4090 Laptop ~150–175 W class |
| `nvidia_datacenter_350` | H100 PCIe default 350 W |
| `nvidia_datacenter_250` | A100 PCIe 250 W |
| `apple_base` | Base M-series; LLM GPU often ~10–13 W |
| `apple_pro` | Pro; GPU load ~20–25 W class |
| `apple_max` | Max; sustained LLM often ~60–90 W system |
| `apple_ultra` | Ultra; sustained LLM wall often ~200–270 W |

Unknown NVIDIA → `nvidia_unknown_desktop` / `nvidia_unknown_laptop`. No GPU identity → `generic_cpu`.

Bounds widen when size is unknown, phase timings are missing, or the call is short. Each result includes `confidence`, `calibration_id`, and lower/upper bounds.

## Display

Always show provenance, e.g.:

- `12.4 J · measured · GPU (nvidia-smi)`
- `3.1 J · measured · CPU package (RAPL)`
- `~90 J (45–140) · approximated · apple_base · low`

## Where it lives

| Piece | Role |
| --- | --- |
| `code/energy/monitor.py` | In-process orchestration |
| `code/energy/collectors.py` | NVIDIA / RAPL sensors |
| `code/energy/approximation.py` | Timing + size-based estimate |
| `code/energy/calibration.py` | TGP/tier watt profiles (v5) |
| `code/energy/ollama_meta.py` | `/api/show`, `/api/tags` |
| `code/energy/platform_probe.py` | Chip / GPU identity (incl. name + power.limit) |
| `code/energy/tracking.py` | Wraps CLI/desktop agent `_call_sync` |
| CLI TUI status bar | Session energy (`code/tui/display.py`) |
| Tauri footer + receipt | Session + per-call via `energy_updated` events |

CLI and Tauri share the Python agent path; energy is emitted as headless `energy_updated` events and included in session `usage`.
