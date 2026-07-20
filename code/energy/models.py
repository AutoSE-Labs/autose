"""Wire and domain types shared by energy sensors, approximation, and the monitor."""

from dataclasses import asdict, dataclass
from typing import Literal

CollectorName = Literal["nvidia-smi", "rapl", "approximation", "none"]
CapabilityStatus = Literal["available", "unsupported", "error"]
MeasurementQuality = Literal["measured", "estimated", "approximated", "unavailable"]
EnergyScope = Literal["gpu", "cpu_package", "modeled_active_device", "none"]
EnergyMethod = Literal[
    "sensor",
    "concurrent_share",
    "model_timing",
    "model_tokens",
    "none",
]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class EnergyCapability:
    platform: str
    architecture: str
    collector: CollectorName
    status: CapabilityStatus
    quality: MeasurementQuality
    scope: EnergyScope
    message: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PowerSample:
    timestamp: float
    watts: float


@dataclass(frozen=True)
class EnergyResult:
    span_id: str
    model: str
    operation: str
    started_at: str
    finished_at: str
    duration_ms: int
    energy_joules: float | None
    average_watts: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    joules_per_token: float | None
    quality: MeasurementQuality
    collector: CollectorName
    scope: EnergyScope = "none"
    method: EnergyMethod = "none"
    confidence: Confidence = "low"
    energy_joules_lower: float | None = None
    energy_joules_upper: float | None = None
    calibration_id: str | None = None
    hardware_bucket: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
