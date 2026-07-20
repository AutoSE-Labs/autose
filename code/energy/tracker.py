"""Span accounting and trapezoidal power integration shared by sensors."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import (
    CollectorName,
    Confidence,
    EnergyMethod,
    EnergyResult,
    EnergyScope,
    MeasurementQuality,
    PowerSample,
)

VALID_OPERATIONS = frozenset({"chat", "embed"})


@dataclass
class ActiveSpan:
    span_id: str
    model: str
    operation: str
    started_wall: datetime
    started_monotonic: float
    energy_joules: float = 0.0
    max_concurrency: int = 1


class SpanTracker:
    """Tracks overlapping inference spans and attributes integrated joules."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, ActiveSpan] = {}
        self._previous_sample: PowerSample | None = None
        self.completed_spans = 0
        self.total_energy_joules = 0.0
        self.total_tokens = 0
        self.approximated_spans = 0
        self.measured_spans = 0

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def begin(self, model: str, operation: str = "chat", span_id: str | None = None) -> str:
        if operation not in VALID_OPERATIONS:
            raise ValueError(f"unsupported operation: {operation}")
        span_id = span_id or str(uuid.uuid4())
        self.flush_to_now()
        with self._lock:
            if span_id in self._active:
                raise ValueError("span already exists")
            self._active[span_id] = ActiveSpan(
                span_id=span_id,
                model=model,
                operation=operation,
                started_wall=datetime.now(UTC),
                started_monotonic=time.monotonic(),
                max_concurrency=max(1, len(self._active) + 1),
            )
        return span_id

    def integrate(self, sample: PowerSample) -> None:
        with self._lock:
            previous = self._previous_sample
            if previous is not None and sample.timestamp <= previous.timestamp:
                return
            self._previous_sample = sample
            if previous is None or not self._active:
                return
            interval_joules = ((previous.watts + sample.watts) / 2) * (
                sample.timestamp - previous.timestamp
            )
            concurrency = len(self._active)
            share = max(0.0, interval_joules) / concurrency
            for span in self._active.values():
                span.energy_joules += share
                span.max_concurrency = max(span.max_concurrency, concurrency)

    def flush_to_now(self) -> None:
        with self._lock:
            previous = self._previous_sample
        if previous is not None:
            self.integrate(PowerSample(time.monotonic(), previous.watts))

    def end_measured(
        self,
        span_id: str,
        *,
        collector: CollectorName,
        scope: EnergyScope,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        sensor_available: bool = True,
    ) -> EnergyResult | None:
        self.flush_to_now()
        with self._lock:
            span = self._active.pop(span_id, None)
        if span is None:
            return None
        finished_wall = datetime.now(UTC)
        duration_ms = max(0, round((time.monotonic() - span.started_monotonic) * 1000))
        token_count = (prompt_tokens or 0) + (completion_tokens or 0)
        energy = span.energy_joules if sensor_available else None
        if energy is None:
            quality: MeasurementQuality = "unavailable"
            method: EnergyMethod = "none"
            confidence: Confidence = "low"
        elif span.max_concurrency > 1:
            quality = "estimated"
            method = "concurrent_share"
            confidence = "medium"
        else:
            quality = "measured"
            method = "sensor"
            confidence = "high"
        result = EnergyResult(
            span_id=span.span_id,
            model=span.model,
            operation=span.operation,
            started_at=span.started_wall.isoformat(),
            finished_at=finished_wall.isoformat(),
            duration_ms=duration_ms,
            energy_joules=energy,
            average_watts=(
                None if energy is None or duration_ms == 0 else energy / (duration_ms / 1000)
            ),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            joules_per_token=None if energy is None or token_count == 0 else energy / token_count,
            quality=quality,
            collector=collector,
            scope=scope if energy is not None else "none",
            method=method,
            confidence=confidence,
        )
        with self._lock:
            self.completed_spans += 1
            if energy is not None:
                self.total_energy_joules += energy
                self.measured_spans += 1
            self.total_tokens += token_count
        return result

    def record_external(self, result: EnergyResult) -> None:
        """Record an approximated (or other) result that did not use sensor spans."""
        with self._lock:
            self.completed_spans += 1
            self.total_tokens += (result.prompt_tokens or 0) + (result.completion_tokens or 0)
            if result.energy_joules is not None:
                self.total_energy_joules += result.energy_joules
            if result.quality == "approximated":
                self.approximated_spans += 1
            elif result.quality in {"measured", "estimated"}:
                self.measured_spans += 1

    def discard(self, span_id: str) -> ActiveSpan | None:
        self.flush_to_now()
        with self._lock:
            return self._active.pop(span_id, None)

    def summary(self, capability: dict[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            total_energy = self.total_energy_joules
            total_tokens = self.total_tokens
            completed = self.completed_spans
            measured = self.measured_spans
            approximated = self.approximated_spans
        return {
            "completed_spans": completed,
            "measured_spans": measured,
            "approximated_spans": approximated,
            "total_energy_joules": total_energy if completed else None,
            "total_tokens": total_tokens,
            "joules_per_token": (
                total_energy / total_tokens if total_energy and total_tokens else None
            ),
            "capability": capability,
        }
