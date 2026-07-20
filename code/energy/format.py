"""Compact display helpers for CLI and desktop energy UI."""

from __future__ import annotations

from typing import Any


def format_joules(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f} kJ"
    if value >= 100:
        return f"{value:.0f} J"
    if value >= 10:
        return f"{value:.1f} J"
    return f"{value:.2f} J"


def format_energy_result(result: Any) -> str:
    """One-line provenance string, e.g. '~31 J · approximated · Apple M4 · medium'."""
    joules = getattr(result, "energy_joules", None)
    quality = getattr(result, "quality", "unavailable")
    scope = getattr(result, "scope", "none")
    confidence = getattr(result, "confidence", None)
    bucket = getattr(result, "hardware_bucket", None)
    collector = getattr(result, "collector", None)
    lower = getattr(result, "energy_joules_lower", None)
    upper = getattr(result, "energy_joules_upper", None)

    if quality == "approximated" and joules is not None:
        core = f"~{format_joules(joules)}"
        if lower is not None and upper is not None:
            core = f"~{format_joules(joules)} ({format_joules(lower)}–{format_joules(upper)})"
    else:
        core = format_joules(joules)

    parts = [core, str(quality)]
    if scope == "gpu":
        parts.append("GPU")
    elif scope == "cpu_package":
        parts.append("CPU")
    elif bucket:
        parts.append(str(bucket).replace("_", " "))
    elif collector and collector not in {"none", "approximation"}:
        parts.append(str(collector))
    if confidence and quality == "approximated":
        parts.append(str(confidence))
    return " · ".join(parts)


def format_energy_status(status: dict[str, Any]) -> str:
    summary = status.get("summary") or {}
    total = summary.get("total_energy_joules")
    quality = status.get("quality") or "unavailable"
    scope = status.get("scope") or "none"
    if total is None:
        return "Energy unavailable"
    label = format_joules(float(total))
    if quality == "approximated":
        return f"~{label} · approx"
    if scope == "gpu":
        return f"{label} · GPU"
    if scope == "cpu_package":
        return f"{label} · CPU"
    return f"{label} · {quality}"
