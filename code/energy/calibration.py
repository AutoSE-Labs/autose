"""Bundled device-specific energy calibration profiles (versioned)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .platform_probe import DeviceIdentity

CALIBRATION_VERSION = "3"


@dataclass(frozen=True)
class HardwareProfile:
    id: str
    prefill_watts: float
    decode_watts: float
    joules_per_prompt_token: float
    joules_per_completion_token: float
    # Extra cost for moving ~1 GiB of weights once per generated token (decode-heavy).
    memory_joules_per_gib_token: float
    relative_uncertainty: float
    notes: str

    @property
    def active_watts(self) -> float:
        """Blend used only for last-resort wall-time fallback."""
        return (self.prefill_watts + 3.0 * self.decode_watts) / 4.0


# Conservative defaults: directional estimates, not meter-grade.
# Apple M4 Air values seeded from local powermetrics compare on Mac16,12
# (gemma4:e2b / ~5B Q4 long decode ~3 W SoC average).
HARDWARE_PROFILES: dict[str, HardwareProfile] = {
    "nvidia_consumer_high": HardwareProfile(
        id="nvidia_consumer_high",
        prefill_watts=280.0,
        decode_watts=220.0,
        joules_per_prompt_token=0.020,
        joules_per_completion_token=0.045,
        memory_joules_per_gib_token=0.0040,
        relative_uncertainty=0.45,
        notes="High-end discrete NVIDIA under local inference load.",
    ),
    "nvidia_consumer_mid": HardwareProfile(
        id="nvidia_consumer_mid",
        prefill_watts=180.0,
        decode_watts=140.0,
        joules_per_prompt_token=0.015,
        joules_per_completion_token=0.035,
        memory_joules_per_gib_token=0.0035,
        relative_uncertainty=0.50,
        notes="Mid-range discrete NVIDIA under local inference load.",
    ),
    "nvidia_laptop": HardwareProfile(
        id="nvidia_laptop",
        prefill_watts=100.0,
        decode_watts=70.0,
        joules_per_prompt_token=0.012,
        joules_per_completion_token=0.028,
        memory_joules_per_gib_token=0.0030,
        relative_uncertainty=0.55,
        notes="Laptop NVIDIA GPU under local inference load.",
    ),
    "apple_m4": HardwareProfile(
        id="apple_m4",
        prefill_watts=5.0,
        decode_watts=3.0,
        joules_per_prompt_token=0.0015,
        joules_per_completion_token=0.0030,
        memory_joules_per_gib_token=0.0012,
        relative_uncertainty=0.40,
        notes="Apple M4 SoC; watts×load_scale + memory×GiB×tokens.",
    ),
    "apple_m3": HardwareProfile(
        id="apple_m3",
        prefill_watts=5.5,
        decode_watts=3.5,
        joules_per_prompt_token=0.0018,
        joules_per_completion_token=0.0035,
        memory_joules_per_gib_token=0.0013,
        relative_uncertainty=0.45,
        notes="Apple M3 SoC approximate profile.",
    ),
    "apple_m2": HardwareProfile(
        id="apple_m2",
        prefill_watts=6.0,
        decode_watts=4.0,
        joules_per_prompt_token=0.0020,
        joules_per_completion_token=0.0040,
        memory_joules_per_gib_token=0.0014,
        relative_uncertainty=0.50,
        notes="Apple M2 SoC approximate profile.",
    ),
    "apple_m1": HardwareProfile(
        id="apple_m1",
        prefill_watts=6.5,
        decode_watts=4.5,
        joules_per_prompt_token=0.0022,
        joules_per_completion_token=0.0045,
        memory_joules_per_gib_token=0.0015,
        relative_uncertainty=0.55,
        notes="Apple M1 SoC approximate profile.",
    ),
    "apple_silicon": HardwareProfile(
        id="apple_silicon",
        prefill_watts=6.0,
        decode_watts=4.0,
        joules_per_prompt_token=0.0020,
        joules_per_completion_token=0.0040,
        memory_joules_per_gib_token=0.0014,
        relative_uncertainty=0.65,
        notes="Generic Apple Silicon fallback when chip string is unknown.",
    ),
    "generic_cpu": HardwareProfile(
        id="generic_cpu",
        prefill_watts=55.0,
        decode_watts=40.0,
        joules_per_prompt_token=0.010,
        joules_per_completion_token=0.025,
        memory_joules_per_gib_token=0.0025,
        relative_uncertainty=0.70,
        notes="Uncalibrated CPU-only / unknown device fallback.",
    ),
}


def resolve_hardware_bucket(
    *,
    platform_name: str | None = None,
    architecture: str | None = None,
    gpu_name: str | None = None,
    device: DeviceIdentity | None = None,
) -> HardwareProfile:
    identity = device or DeviceIdentity(
        platform_name=(platform_name or "").lower(),
        architecture=architecture or "",
        gpu_name=gpu_name,
    )
    gpu = (identity.gpu_name or gpu_name or "").lower()
    if gpu:
        if any(token in gpu for token in ("4090", "4080", "3090", "a6000", "a100", "h100")):
            return HARDWARE_PROFILES["nvidia_consumer_high"]
        if any(token in gpu for token in ("4070", "4060", "3080", "3070", "3060", "a5000")):
            return HARDWARE_PROFILES["nvidia_consumer_mid"]
        if "nvidia" in gpu or "geforce" in gpu or "rtx" in gpu or "quadro" in gpu:
            if any(token in gpu for token in ("laptop", "max-q", "mobile")):
                return HARDWARE_PROFILES["nvidia_laptop"]
            return HARDWARE_PROFILES["nvidia_consumer_mid"]

    if identity.platform_name == "darwin":
        return _apple_profile(identity)

    return HARDWARE_PROFILES["generic_cpu"]


def _apple_profile(identity: DeviceIdentity) -> HardwareProfile:
    text = " ".join(
        part for part in (identity.chip_name, identity.hw_model) if part
    ).lower()
    match = re.search(r"\bm([1-4])(?:\s*(pro|max|ultra))?\b", text)
    if match:
        generation = match.group(1)
        return HARDWARE_PROFILES.get(f"apple_m{generation}", HARDWARE_PROFILES["apple_silicon"])
    if identity.hw_model:
        model_match = re.match(r"Mac(\d+),", identity.hw_model)
        if model_match:
            major = int(model_match.group(1))
            if major >= 16:
                return HARDWARE_PROFILES["apple_m4"]
            if major >= 15:
                return HARDWARE_PROFILES["apple_m3"]
            if major >= 14:
                return HARDWARE_PROFILES["apple_m2"]
            if major >= 13:
                return HARDWARE_PROFILES["apple_m1"]
    return HARDWARE_PROFILES["apple_silicon"]
