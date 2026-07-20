"""User-level hardware energy collectors. No elevated tools."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .models import CapabilityStatus, CollectorName, EnergyCapability, EnergyScope, PowerSample


class EnergyCollector(Protocol):
    capability: EnergyCapability

    def sample(self) -> PowerSample: ...


def _capability(
    collector: CollectorName,
    status: CapabilityStatus,
    scope: EnergyScope,
    message: str,
) -> EnergyCapability:
    return EnergyCapability(
        platform=platform.system().lower(),
        architecture=platform.machine(),
        collector=collector,
        status=status,
        quality="measured" if status == "available" else "unavailable",
        scope=scope if status == "available" else "none",
        message=message,
    )


class UnavailableCollector:
    def __init__(self, capability: EnergyCapability) -> None:
        self.capability = capability

    def sample(self) -> PowerSample:
        raise RuntimeError(self.capability.message)


class NvidiaSmiCollector:
    def __init__(self, binary: Path) -> None:
        self._binary = binary
        self.capability = _capability(
            "nvidia-smi",
            "available",
            "gpu",
            "NVIDIA GPU board power is available through nvidia-smi.",
        )

    def sample(self) -> PowerSample:
        completed = subprocess.run(
            [
                str(self._binary),
                "--query-gpu=power.draw",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        readings: list[float] = []
        for line in completed.stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            lowered = text.lower()
            if "not supported" in lowered or "n/a" in lowered:
                continue
            readings.append(float(text))
        if not readings:
            raise RuntimeError("nvidia-smi returned no usable power readings")
        return PowerSample(time.monotonic(), sum(readings))

    def gpu_name(self) -> str | None:
        try:
            completed = subprocess.run(
                [
                    str(self._binary),
                    "--query-gpu=name",
                    "--format=csv,noheader",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return ", ".join(names) if names else None


class RaplCollector:
    def __init__(self, energy_files: list[Path]) -> None:
        self._energy_files = energy_files
        self._previous: tuple[float, float] | None = None
        self.capability = _capability(
            "rapl",
            "available",
            "cpu_package",
            "CPU package energy is available through Linux RAPL.",
        )

    def sample(self) -> PowerSample:
        timestamp = time.monotonic()
        joules = sum(float(path.read_text().strip()) / 1_000_000 for path in self._energy_files)
        previous = self._previous
        self._previous = (timestamp, joules)
        if previous is None or timestamp <= previous[0] or joules < previous[1]:
            return PowerSample(timestamp, 0.0)
        return PowerSample(timestamp, (joules - previous[1]) / (timestamp - previous[0]))


def _trusted_executable(paths: tuple[str, ...]) -> Path | None:
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file() and os.access(path, os.X_OK):
            return path
    which = shutil.which("nvidia-smi")
    if which:
        candidate = Path(which)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _nvidia_smi_candidates() -> tuple[str, ...]:
    system = platform.system().lower()
    if system == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return (
            str(Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"),
            str(Path(program_files_x86) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"),
            r"C:\Windows\System32\nvidia-smi.exe",
        )
    return ("/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi")


def _readable_rapl_files() -> list[Path]:
    root = Path("/sys/class/powercap")
    if not root.is_dir():
        return []
    return [
        energy_file
        for zone in root.glob("intel-rapl:[0-9]*")
        if re.fullmatch(r"intel-rapl:\d+", zone.name)
        and (energy_file := zone / "energy_uj").is_file()
        and os.access(energy_file, os.R_OK)
    ]


def detect_nvidia_binary() -> Path | None:
    return _trusted_executable(_nvidia_smi_candidates())


def detect_collector() -> EnergyCollector:
    """Pick the first usable user-level sensor. Never uses elevated tools."""
    system = platform.system().lower()

    nvidia = detect_nvidia_binary()
    if nvidia and system in {"linux", "windows"}:
        collector = NvidiaSmiCollector(nvidia)
        try:
            collector.sample()
            return collector
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    if system == "linux":
        rapl_files = _readable_rapl_files()
        if rapl_files:
            return RaplCollector(rapl_files)
        return UnavailableCollector(
            _capability(
                "none",
                "unsupported",
                "none",
                "No readable NVIDIA or Intel RAPL energy source was detected.",
            )
        )

    if system == "darwin":
        return UnavailableCollector(
            _capability(
                "approximation",
                "unsupported",
                "none",
                "No user-level hardware power sensor on macOS; using model/timing approximation.",
            )
        )

    if system == "windows":
        return UnavailableCollector(
            _capability(
                "none",
                "unsupported",
                "none",
                "No usable NVIDIA power.draw telemetry; using model/timing approximation.",
            )
        )

    return UnavailableCollector(
        _capability(
            "none",
            "unsupported",
            "none",
            f"Energy measurement is not supported on {system}/{platform.machine()}.",
        )
    )
