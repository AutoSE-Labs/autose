"""Model/timing-based energy approximation when hardware sensors are unavailable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re

from .calibration import CALIBRATION_VERSION, HardwareProfile, resolve_hardware_bucket
from .models import Confidence, EnergyResult
from .platform_probe import DeviceIdentity, detect_device_identity

_PARAM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([bBmM])")
_MOE_RE = re.compile(r"(\d+)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[bB]")

# Fallback footprint when Ollama size_bytes is unavailable (~7B Q4).
_REF_ACTIVE_B = 7.0
_REF_QUANT_BITS = 4.0
_SHORT_CALL_MS = 5000


@dataclass(frozen=True)
class ModelSignals:
    name: str
    parameter_size: str | None = None
    quantization_level: str | None = None
    family: str | None = None
    parameter_billions: float | None = None
    active_parameter_billions: float | None = None
    is_moe: bool = False
    quant_bits: float | None = None
    size_bytes: int | None = None


@dataclass(frozen=True)
class TimingSignals:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    load_duration_ns: int | None = None
    prompt_eval_duration_ns: int | None = None
    eval_duration_ns: int | None = None
    total_duration_ns: int | None = None
def parse_model_signals(
    model: str,
    *,
    parameter_size: str | None = None,
    quantization_level: str | None = None,
    family: str | None = None,
    parameter_count: int | None = None,
    size_bytes: int | None = None,
) -> ModelSignals:
    text = " ".join(part for part in (model, parameter_size, family) if part)
    is_moe = bool(_MOE_RE.search(text) or (family and "moe" in family.lower()))
    parameter_billions = _parse_billions(parameter_size) or _parse_billions(model)
    if parameter_billions is None and isinstance(parameter_count, int) and parameter_count > 0:
        parameter_billions = parameter_count / 1_000_000_000
    active = parameter_billions
    moe_match = _MOE_RE.search(text)
    if moe_match:
        experts = float(moe_match.group(1))
        expert_size = float(moe_match.group(2))
        # Approximate Mixtral-style: ~2 experts active of N.
        active = expert_size * min(2.0, experts)
        parameter_billions = parameter_billions or experts * expert_size
        is_moe = True
    resolved_size = size_bytes if isinstance(size_bytes, int) and size_bytes > 0 else None
    return ModelSignals(
        name=model,
        parameter_size=parameter_size,
        quantization_level=quantization_level,
        family=family,
        parameter_billions=parameter_billions,
        active_parameter_billions=active,
        is_moe=is_moe,
        quant_bits=_parse_quant_bits(quantization_level) or _parse_quant_bits(model),
        size_bytes=resolved_size,
    )


def _parse_billions(value: str | None) -> float | None:
    if not value:
        return None
    match = _PARAM_RE.search(value.replace(",", ""))
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    return amount if unit == "b" else amount / 1000.0


def _parse_quant_bits(value: str | None) -> float | None:
    if not value:
        return None
    text = value.upper()
    for pattern, bits in (
        (r"Q8_0|Q8", 8.0),
        (r"Q6", 6.0),
        (r"Q5", 5.0),
        (r"Q4|IQ4", 4.0),
        (r"Q3|IQ3", 3.0),
        (r"Q2|IQ2", 2.0),
        (r"FP16|F16", 16.0),
        (r"FP32|F32", 32.0),
        (r"FP8|F8", 8.0),
    ):
        if re.search(pattern, text):
            return bits
    return None


def model_bytes_gib(signals: ModelSignals) -> float:
    """Prefer Ollama blob size; else estimate from params × quant bits."""
    if signals.size_bytes is not None and signals.size_bytes > 0:
        return max(0.05, signals.size_bytes / (1024.0**3))
    # MoE: resident footprint ≈ total params; active used only if total unknown.
    params = signals.parameter_billions or signals.active_parameter_billions or _REF_ACTIVE_B
    quant = signals.quant_bits or _REF_QUANT_BITS
    return max(0.05, params * (quant / 8.0))


def approximate_energy(
    *,
    span_id: str,
    model: str,
    operation: str,
    started_at: str,
    duration_ms: int,
    timings: TimingSignals,
    model_signals: ModelSignals,
    gpu_name: str | None = None,
    device: DeviceIdentity | None = None,
) -> EnergyResult:
    """
    Approximate **warm** inference energy without hardware sensors.

    E ≈ P_phase·t_phase + c_mem·GiB·T_tokens

    Assumes the model is already loaded (normal steady use). Cold load / first
    pull into memory is out of scope. Device watts are not scaled by model size —
    wall/phase time already reflects heavier models; size enters via memory traffic.
    """
    identity = device or detect_device_identity(gpu_name)
    if gpu_name and identity.gpu_name != gpu_name:
        identity = DeviceIdentity(
            platform_name=identity.platform_name,
            architecture=identity.architecture,
            hw_model=identity.hw_model,
            chip_name=identity.chip_name,
            gpu_name=gpu_name,
        )
    profile = resolve_hardware_bucket(device=identity, gpu_name=gpu_name)
    gib = model_bytes_gib(model_signals)
    known_size = model_signals.size_bytes is not None
    known_model = known_size or model_signals.parameter_billions is not None

    # Ignore load_duration — warm-only: charge inference phases / wall time.
    prompt_s = _ns_to_seconds(timings.prompt_eval_duration_ns)
    decode_s = _ns_to_seconds(timings.eval_duration_ns)
    total_s = _ns_to_seconds(timings.total_duration_ns)
    if total_s is None and duration_ms > 0:
        total_s = duration_ms / 1000.0

    has_phase = prompt_s is not None or decode_s is not None
    method = "model_tokens"
    confidence: Confidence = "low"

    mem_j = _memory_token_energy(
        profile,
        gib,
        timings.prompt_tokens or 0,
        timings.completion_tokens or 0,
    )

    if has_phase:
        energy = (
            profile.prefill_watts * max(0.0, prompt_s or 0.0)
            + profile.base_watts * max(0.0, decode_s or 0.0)
            + mem_j
        )
        phase_sum = max(0.0, prompt_s or 0.0) + max(0.0, decode_s or 0.0)
        if total_s is not None and total_s > phase_sum + 0.05:
            energy += profile.base_watts * max(0.0, total_s - phase_sum)
        method = "model_timing"
        confidence = "medium" if (prompt_s is not None and decode_s is not None and known_model) else "low"
    elif total_s is not None and total_s > 0:
        energy = profile.active_watts * total_s + mem_j
        method = "model_timing"
        confidence = "low"
    else:
        prompt_tokens = timings.prompt_tokens or 0
        completion_tokens = timings.completion_tokens or 0
        energy = (
            profile.joules_per_prompt_token * prompt_tokens
            + profile.joules_per_completion_token * completion_tokens
            + mem_j
        )
        if prompt_tokens + completion_tokens == 0:
            energy = profile.active_watts * (duration_ms / 1000.0)
        confidence = "low"

    if profile.id in {"generic_cpu", "apple_silicon"} or not known_model:
        confidence = "low"
    elif method == "model_timing" and known_model and has_phase and (
        profile.id.startswith("apple_m") or profile.id.startswith("nvidia_")
    ):
        confidence = "medium"

    uncertainty = _uncertainty(
        profile,
        confidence=confidence,
        known_size=known_size,
        has_phase=has_phase,
        duration_ms=duration_ms,
    )
    lower = max(0.0, energy * (1.0 - uncertainty))
    upper = energy * (1.0 + uncertainty)
    token_count = (timings.prompt_tokens or 0) + (timings.completion_tokens or 0)

    return EnergyResult(
        span_id=span_id,
        model=model,
        operation=operation,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        duration_ms=duration_ms,
        energy_joules=energy,
        average_watts=None if duration_ms == 0 else energy / (duration_ms / 1000),
        prompt_tokens=timings.prompt_tokens,
        completion_tokens=timings.completion_tokens,
        joules_per_token=None if token_count == 0 else energy / token_count,
        quality="approximated",
        collector="approximation",
        scope="modeled_active_device",
        method=method,
        confidence=confidence,
        energy_joules_lower=lower,
        energy_joules_upper=upper,
        calibration_id=f"{CALIBRATION_VERSION}:{profile.id}",
        hardware_bucket=profile.id,
    )


def _memory_token_energy(
    profile: HardwareProfile,
    gib: float,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    # Prefill reuses weights across tokens more than decode.
    weighted_tokens = 0.25 * max(0, prompt_tokens) + max(0, completion_tokens)
    return profile.memory_joules_per_gib_token * gib * weighted_tokens


def _uncertainty(
    profile: HardwareProfile,
    *,
    confidence: Confidence,
    known_size: bool,
    has_phase: bool,
    duration_ms: int,
) -> float:
    uncertainty = profile.relative_uncertainty
    if confidence == "low":
        uncertainty = min(0.90, uncertainty + 0.15)
    if not known_size:
        uncertainty = min(0.90, uncertainty + 0.10)
    if not has_phase:
        uncertainty = min(0.90, uncertainty + 0.10)
    if duration_ms < _SHORT_CALL_MS:
        uncertainty = min(0.90, uncertainty + 0.10)
    return uncertainty


def _ns_to_seconds(value: int | None) -> float | None:
    if value is None or value < 0:
        return None
    return value / 1_000_000_000
