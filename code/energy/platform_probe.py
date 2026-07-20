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


@lru_cache(maxsize=1)
def detect_device_identity(gpu_name: str | None = None) -> DeviceIdentity:
    platform_name = platform.system().lower()
    architecture = platform.machine()
    hw_model = None
    chip_name = None
    if platform_name == "darwin":
        hw_model = _sysctl("hw.model")
        chip_name = _sysctl("machdep.cpu.brand_string") or _parse_chip_from_profiler()
    return DeviceIdentity(
        platform_name=platform_name,
        architecture=architecture,
        hw_model=hw_model,
        chip_name=chip_name,
        gpu_name=gpu_name,
    )


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
