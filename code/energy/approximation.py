"""Model/timing-based energy approximation when hardware sensors are unavailable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re

from .calibration import CALIBRATION_VERSION, resolve_hardware_bucket
from .models import Confidence, EnergyResult
from .platform_probe import DeviceIdentity, detect_device_identity

_PARAM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([bBmM])")
_MOE_RE = re.compile(r"(\d+)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[bB]")


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


@dataclass(frozen=True)
class TimingSignals:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_eval_duration_ns: int | None = None
    eval_duration_ns: int | None = None
    total_duration_ns: int | None = None


def parse_model_signals(
    model: str,
    *,
    parameter_size: str | None = None,
    quantization_level: str | None = None,
    family: str | None = None,
) -> ModelSignals:
    text = " ".join(part for part in (model, parameter_size, family) if part)
    is_moe = bool(_MOE_RE.search(text) or (family and "moe" in family.lower()))
    parameter_billions = _parse_billions(parameter_size) or _parse_billions(model)
    active = parameter_billions
    moe_match = _MOE_RE.search(text)
    if moe_match:
        experts = float(moe_match.group(1))
        expert_size = float(moe_match.group(2))
        # Approximate Mixtral-style: ~2 experts active of N.
        active = expert_size * min(2.0, experts)
        parameter_billions = parameter_billions or experts * expert_size
        is_moe = True
    return ModelSignals(
        name=model,
        parameter_size=parameter_size,
        quantization_level=quantization_level,
        family=family,
        parameter_billions=parameter_billions,
        active_parameter_billions=active,
        is_moe=is_moe,
        quant_bits=_parse_quant_bits(quantization_level) or _parse_quant_bits(model),
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

    prompt_s = _ns_to_seconds(timings.prompt_eval_duration_ns)
    decode_s = _ns_to_seconds(timings.eval_duration_ns)
    method = "model_tokens"
    confidence: Confidence = "low"

    if prompt_s is not None or decode_s is not None:
        # Duration already reflects model size/quant/MoE. Do not scale watts by params.
        energy = (
            profile.prefill_watts * (prompt_s or 0.0)
            + profile.decode_watts * (decode_s or 0.0)
        )
        if energy <= 0 and timings.total_duration_ns:
            total_s = _ns_to_seconds(timings.total_duration_ns) or 0.0
            energy = profile.active_watts * total_s
        method = "model_timing"
        confidence = "medium" if (prompt_s is not None and decode_s is not None) else "low"
    elif timings.total_duration_ns:
        # OpenAI-compatible APIs often omit phase timings; use wall/total duration.
        total_s = _ns_to_seconds(timings.total_duration_ns) or 0.0
        energy = profile.active_watts * max(0.0, total_s)
        method = "model_timing"
        confidence = "low"
    else:
        prompt_tokens = timings.prompt_tokens or 0
        completion_tokens = timings.completion_tokens or 0
        energy = (
            profile.joules_per_prompt_token * prompt_tokens
            + profile.joules_per_completion_token * completion_tokens
        )
        if prompt_tokens + completion_tokens == 0:
            energy = profile.active_watts * (duration_ms / 1000.0)
        confidence = "low"

    if profile.id in {"generic_cpu", "apple_silicon"}:
        confidence = "low"
    elif method == "model_timing" and profile.id.startswith("apple_m"):
        confidence = "medium"

    uncertainty = profile.relative_uncertainty
    if confidence == "low":
        uncertainty = min(0.85, uncertainty + 0.15)
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


def _ns_to_seconds(value: int | None) -> float | None:
    if value is None or value < 0:
        return None
    return value / 1_000_000_000
