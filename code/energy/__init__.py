"""Optional energy measurement and approximation for local model inference."""

from .approximation import approximate_energy, parse_model_signals
from .collectors import detect_collector
from .format import format_energy_result, format_energy_status, format_joules
from .models import EnergyCapability, EnergyResult
from .monitor import EnergyMonitor, get_default_monitor
from .platform_probe import DeviceIdentity, detect_device_identity

__all__ = [
    "DeviceIdentity",
    "EnergyCapability",
    "EnergyMonitor",
    "EnergyResult",
    "approximate_energy",
    "detect_collector",
    "detect_device_identity",
    "format_energy_result",
    "format_energy_status",
    "format_joules",
    "get_default_monitor",
    "parse_model_signals",
]
