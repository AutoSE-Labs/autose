"""Unprivileged hardware identity probes for approximation buckets."""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class DeviceIdentity:
    platform_name: str
    architecture: str
    hw_model: str | None = None
    chip_name: str | None = None
    gpu_name: str | None = None
    # From nvidia-smi power.limit when available (watts); used to pick TGP band.
    gpu_power_limit_w: float | None = None


def detect_device_identity(
    gpu_name: str | None = None,
    gpu_power_limit_w: float | None = None,
) -> DeviceIdentity:
    platform_name = platform.system().lower()
    architecture = platform.machine()
    hw_model = None
    chip_name = None
    if platform_name == "darwin":
        hw_model = _sysctl("hw.model")
        chip_name = _sysctl("machdep.cpu.brand_string") or _parse_chip_from_profiler()

    probed_name: str | None = None
    probed_limit: float | None = None
    if gpu_name is None or gpu_power_limit_w is None:
        probed_name, probed_limit = probe_discrete_gpu()

    return DeviceIdentity(
        platform_name=platform_name,
        architecture=architecture,
        hw_model=hw_model,
        chip_name=chip_name,
        gpu_name=gpu_name or probed_name,
        gpu_power_limit_w=(
            gpu_power_limit_w if gpu_power_limit_w is not None else probed_limit
        ),
    )


@lru_cache(maxsize=1)
def probe_discrete_gpu() -> tuple[str | None, float | None]:
    """
    Best-effort GPU name (+ optional power limit) without requiring power.draw.

    Order: nvidia-smi query → Windows CIM video controller name.
    """
    name, limit = _probe_nvidia_smi_identity()
    if name:
        return name, limit
    if platform.system().lower() == "windows":
        return _probe_windows_video_controller(), None
    return None, None


def _probe_nvidia_smi_identity() -> tuple[str | None, float | None]:
    # Local import avoids collectors↔platform_probe cycles at module import time.
    from .collectors import detect_nvidia_binary

    binary = detect_nvidia_binary()
    if binary is None:
        return None, None
    try:
        completed = subprocess.run(
            [
                str(binary),
                "--query-gpu=name,power.limit",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None

    names: list[str] = []
    limits: list[float] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split(",")]
        if not parts:
            continue
        if parts[0]:
            names.append(parts[0])
        if len(parts) > 1:
            raw = parts[1].lower()
            if raw and "not supported" not in raw and "n/a" not in raw:
                try:
                    limits.append(float(raw))
                except ValueError:
                    pass
    if not names:
        return None, None
    return ", ".join(names), (sum(limits) / len(limits) if limits else None)


def _probe_windows_video_controller() -> str | None:
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_VideoController | "
                    "Select-Object -ExpandProperty Name"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not names:
        return None
    # Prefer discrete NVIDIA/AMD names over generic "Microsoft Basic Display".
    for name in names:
        lowered = name.lower()
        if any(token in lowered for token in ("nvidia", "geforce", "rtx", "quadro", "radeon", "amd")):
            return name
    return names[0]


def _sysctl(name: str) -> str | None:
    try:
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _parse_chip_from_profiler() -> str | None:
    try:
        completed = subprocess.run(
            ["system_profiler", "SPHardwareDataType"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Chip:\s*(.+)", completed.stdout)
    return match.group(1).strip() if match else None
