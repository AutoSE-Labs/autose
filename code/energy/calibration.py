"""Bundled device-family energy calibration profiles (versioned).

Watt midpoints are derived from published TGP/TDP (NVIDIA) and measured/reported
SoC or LLM-load ranges (Apple Silicon). They are directional family defaults,
not per-machine fits. Sources noted on each profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .platform_probe import DeviceIdentity

CALIBRATION_VERSION = "5"


@dataclass(frozen=True)
class HardwareProfile:
    id: str
    # Sustained device draw during warm inference (not scaled by model size).
    base_watts: float
    # Prefill often slightly higher than decode; used only when phase timings exist.
    prefill_watts: float
    # Reserved for optional cold-load accounting (unused in warm-only path).
    load_joules_per_gib: float
    joules_per_prompt_token: float
    joules_per_completion_token: float
    memory_joules_per_gib_token: float
    relative_uncertainty: float
    notes: str

    @property
    def active_watts(self) -> float:
        """Blend used when only wall-clock duration is available."""
        return (self.prefill_watts + 3.0 * self.base_watts) / 4.0


def _profile(
    id: str,
    *,
    base: float,
    prefill: float,
    uncertainty: float,
    notes: str,
    mem: float = 0.0030,
) -> HardwareProfile:
    return HardwareProfile(
        id=id,
        base_watts=base,
        prefill_watts=prefill,
        load_joules_per_gib=max(2.0, base * 0.03),
        joules_per_prompt_token=base * 0.00008,
        joules_per_completion_token=base * 0.00018,
        memory_joules_per_gib_token=mem,
        relative_uncertainty=uncertainty,
        notes=notes,
    )


# TGP/TDP and SoC figures from public specs / measurements (see profile notes).
HARDWARE_PROFILES: dict[str, HardwareProfile] = {
    # NVIDIA desktop — official Total Graphics Power (NVIDIA compare / product pages).
    # base ≈ NVIDIA "Average Gaming Power" where published, else ~0.7× TGP.
    "nvidia_desktop_450": _profile(
        "nvidia_desktop_450",
        base=315.0,  # RTX 4090 avg gaming power (NVIDIA); TGP 450 W
        prefill=400.0,
        uncertainty=0.40,
        notes="Desktop ~450 W TGP (RTX 4090). NVIDIA avg gaming ~315 W.",
        mem=0.0040,
    ),
    "nvidia_desktop_320": _profile(
        "nvidia_desktop_320",
        base=250.0,  # RTX 4080 avg gaming ~246–251 W; TGP 320 W. Also RTX 3080 TGP 320 W.
        prefill=300.0,
        uncertainty=0.40,
        notes="Desktop ~320 W TGP (RTX 4080/4080 SUPER, RTX 3080).",
        mem=0.0038,
    ),
    "nvidia_desktop_280": _profile(
        "nvidia_desktop_280",
        base=220.0,  # RTX 4070 Ti / Ti SUPER TGP 285 W
        prefill=270.0,
        uncertainty=0.42,
        notes="Desktop ~285 W TGP (RTX 4070 Ti / Ti SUPER).",
        mem=0.0036,
    ),
    "nvidia_desktop_200": _profile(
        "nvidia_desktop_200",
        base=160.0,  # RTX 4070 TGP 200 W; RTX 3070 TGP 220 W
        prefill=200.0,
        uncertainty=0.45,
        notes="Desktop ~200–220 W TGP (RTX 4070 / 4070 SUPER, RTX 3070).",
        mem=0.0034,
    ),
    "nvidia_desktop_160": _profile(
        "nvidia_desktop_160",
        base=130.0,  # RTX 4060 Ti TGP 160–165 W; RTX 3060 desktop ~170 W
        prefill=160.0,
        uncertainty=0.45,
        notes="Desktop ~160–170 W TGP (RTX 4060 Ti, RTX 3060 class).",
        mem=0.0032,
    ),
    "nvidia_desktop_115": _profile(
        "nvidia_desktop_115",
        base=95.0,  # RTX 4060 TGP 115 W; NVIDIA avg gaming ~110 W
        prefill=115.0,
        uncertainty=0.48,
        notes="Desktop ~115 W TGP (RTX 4060).",
        mem=0.0030,
    ),
    # NVIDIA laptop — configurable TGP bands (NVIDIA / Notebookcheck / ASUS ROG tables).
    "nvidia_laptop_175": _profile(
        "nvidia_laptop_175",
        base=130.0,  # RTX 4080/4090 Laptop up to 150–175 W + Dynamic Boost
        prefill=160.0,
        uncertainty=0.50,
        notes="Laptop high TGP ~150–175 W class (RTX 4080/4090 Laptop).",
        mem=0.0032,
    ),
    "nvidia_laptop_140": _profile(
        "nvidia_laptop_140",
        base=100.0,  # 4050/4060/4070 Laptop max ~115 W + 25 W DB = 140 W
        prefill=125.0,
        uncertainty=0.50,
        notes="Laptop ~115–140 W TGP class (RTX 4050/4060/4070 Laptop high).",
        mem=0.0030,
    ),
    "nvidia_laptop_100": _profile(
        "nvidia_laptop_100",
        base=75.0,  # Common mid laptop configs ~85–115 W (ASUS ROG tables)
        prefill=95.0,
        uncertainty=0.52,
        notes="Laptop mid TGP ~85–115 W class.",
        mem=0.0028,
    ),
    "nvidia_laptop_65": _profile(
        "nvidia_laptop_65",
        base=50.0,  # Thin/light configs ~65 W (e.g. Zephyrus G14 class)
        prefill=65.0,
        uncertainty=0.55,
        notes="Laptop low TGP ~35–65 W class (thin-and-light).",
        mem=0.0026,
    ),
    "nvidia_datacenter_350": _profile(
        "nvidia_datacenter_350",
        base=280.0,  # H100 PCIe default TGP 350 W (NVIDIA product brief)
        prefill=330.0,
        uncertainty=0.40,
        notes="Datacenter ~350 W TGP (H100 PCIe default).",
        mem=0.0040,
    ),
    "nvidia_datacenter_250": _profile(
        "nvidia_datacenter_250",
        base=200.0,  # A100 PCIe 250 W (NVIDIA product brief)
        prefill=240.0,
        uncertainty=0.42,
        notes="Datacenter ~250 W TGP (A100 PCIe).",
        mem=0.0038,
    ),
    "nvidia_unknown_desktop": _profile(
        "nvidia_unknown_desktop",
        base=160.0,
        prefill=200.0,
        uncertainty=0.65,
        notes="Unknown desktop NVIDIA; mid-range default.",
    ),
    "nvidia_unknown_laptop": _profile(
        "nvidia_unknown_laptop",
        base=75.0,
        prefill=95.0,
        uncertainty=0.70,
        notes="Unknown laptop NVIDIA; mid TGP default.",
    ),
    # Apple Silicon — tier from chip string. Midpoints from published SoC peak /
    # LLM-load reports (MacNerd peak table; Notebookcheck GPU; LLM blog measurements).
    "apple_base": _profile(
        "apple_base",
        base=10.0,  # M4 Air/Mini LLM GPU ~11–13 W; MacNerd M4 peak ~35 W package
        prefill=14.0,
        uncertainty=0.45,
        notes="Apple base SoC (Air/Mini class). LLM GPU often ~10–13 W.",
        mem=0.0020,
    ),
    "apple_pro": _profile(
        "apple_pro",
        base=22.0,  # M4 Pro GPU ~20–25 W (Notebookcheck); 8B decode reports ~32 W
        prefill=30.0,
        uncertainty=0.45,
        notes="Apple Pro SoC. GPU load ~20–25 W; LLM reports ~20–35 W.",
        mem=0.0022,
    ),
    "apple_max": _profile(
        "apple_max",
        base=55.0,  # M4 Max LLM system often 60–90 W; MacNerd peak ~145 W
        prefill=75.0,
        uncertainty=0.50,
        notes="Apple Max SoC. Sustained LLM often ~60–90 W system / ~55 W active class.",
        mem=0.0024,
    ),
    "apple_ultra": _profile(
        "apple_ultra",
        base=120.0,  # M3 Ultra LLM wall ~200–270 W; MacNerd peak ~200 W
        prefill=160.0,
        uncertainty=0.55,
        notes="Apple Ultra SoC. Sustained LLM wall often ~200–270 W.",
        mem=0.0026,
    ),
    "apple_silicon": _profile(
        "apple_silicon",
        base=12.0,
        prefill=16.0,
        uncertainty=0.65,
        notes="Generic Apple Silicon when tier unknown.",
        mem=0.0022,
    ),
    "generic_cpu": _profile(
        "generic_cpu",
        base=40.0,
        prefill=55.0,
        uncertainty=0.75,
        notes="CPU-only / unknown device fallback.",
        mem=0.0025,
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
    if gpu and _looks_nvidia(gpu):
        return _nvidia_profile(gpu, identity.gpu_power_limit_w)

    if identity.platform_name == "darwin" or _looks_apple(
        " ".join(part for part in (identity.chip_name, identity.hw_model) if part)
    ):
        return _apple_profile(identity)

    return HARDWARE_PROFILES["generic_cpu"]


def _looks_nvidia(text: str) -> bool:
    return any(token in text for token in ("nvidia", "geforce", "rtx", "quadro", "tesla", "a100", "h100", "a6000"))


def _looks_apple(text: str) -> bool:
    return bool(re.search(r"\bm[1-4]\b|apple", text.lower()))


def _nvidia_profile(gpu: str, power_limit_w: float | None) -> HardwareProfile:
    laptop = any(token in gpu for token in ("laptop", "max-q", "maxq", "mobile"))

    if power_limit_w is not None and power_limit_w > 0:
        return HARDWARE_PROFILES[_band_from_power_limit(power_limit_w, laptop=laptop)]

    if any(token in gpu for token in ("h100", "h200")):
        return HARDWARE_PROFILES["nvidia_datacenter_350"]
    if "a100" in gpu:
        return HARDWARE_PROFILES["nvidia_datacenter_250"]
    if any(token in gpu for token in ("a6000", "a5000", "rtx 6000")):
        return HARDWARE_PROFILES["nvidia_desktop_320"]

    # Explicit SKU → TGP band (desktop unless laptop token present).
    # Longer tokens first so "4070 ti" wins over "4070".
    sku_desktop = (
        (("4090",), "nvidia_desktop_450"),
        (("4080", "3090", "3080"), "nvidia_desktop_320"),
        (("4070 ti", "4070ti"), "nvidia_desktop_280"),
        (("4070", "3070 ti", "3070ti", "3070"), "nvidia_desktop_200"),
        (("4060 ti", "4060ti", "3060 ti", "3060ti", "3060"), "nvidia_desktop_160"),
        (("4060", "3050"), "nvidia_desktop_115"),
    )
    sku_laptop = (
        (("4090", "4080"), "nvidia_laptop_175"),
        (("4070", "4060", "4050", "3080", "3070"), "nvidia_laptop_140"),
        (("3060", "3050"), "nvidia_laptop_100"),
    )
    table = sku_laptop if laptop else sku_desktop
    for tokens, profile_id in table:
        if any(token in gpu for token in tokens):
            return HARDWARE_PROFILES[profile_id]

    if laptop:
        return HARDWARE_PROFILES["nvidia_unknown_laptop"]
    return HARDWARE_PROFILES["nvidia_unknown_desktop"]


def _band_from_power_limit(limit_w: float, *, laptop: bool) -> str:
    if laptop:
        if limit_w >= 150:
            return "nvidia_laptop_175"
        if limit_w >= 110:
            return "nvidia_laptop_140"
        if limit_w >= 80:
            return "nvidia_laptop_100"
        return "nvidia_laptop_65"
    if limit_w >= 400:
        return "nvidia_desktop_450"
    if limit_w >= 300:
        return "nvidia_desktop_320"
    if limit_w >= 250:
        return "nvidia_desktop_280"
    if limit_w >= 180:
        return "nvidia_desktop_200"
    if limit_w >= 140:
        return "nvidia_desktop_160"
    return "nvidia_desktop_115"


def _apple_profile(identity: DeviceIdentity) -> HardwareProfile:
    text = " ".join(
        part for part in (identity.chip_name, identity.hw_model) if part
    ).lower()
    match = re.search(r"\bm([1-4])(?:\s*(pro|max|ultra))?\b", text)
    if match:
        tier = match.group(2)
        if tier == "ultra":
            return HARDWARE_PROFILES["apple_ultra"]
        if tier == "max":
            return HARDWARE_PROFILES["apple_max"]
        if tier == "pro":
            return HARDWARE_PROFILES["apple_pro"]
        return HARDWARE_PROFILES["apple_base"]
    # Mac model identifiers when chip string missing (consumer Macs → base class).
    if identity.hw_model and re.match(r"Mac\d+,", identity.hw_model):
        return HARDWARE_PROFILES["apple_base"]
    return HARDWARE_PROFILES["apple_silicon"]
