"""In-process energy monitor: user-level sensors first, then approximation."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from .approximation import TimingSignals, approximate_energy, parse_model_signals
from .collectors import EnergyCollector, NvidiaSmiCollector, detect_collector
from .models import EnergyCapability, EnergyResult
from .tracker import SpanTracker


class EnergyMonitor:
    """Fail-open monitor used by the runtime around Ollama calls."""

    def __init__(
        self,
        collector: EnergyCollector | None = None,
        sample_interval: float = 0.5,
    ) -> None:
        self.collector = collector if collector is not None else detect_collector()
        self.sample_interval = max(0.25, sample_interval)
        self.tracker = SpanTracker()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sampler_thread: threading.Thread | None = None
        self._gpu_name: str | None = None
        self._model_cache: dict[str, dict[str, str | None]] = {}
        if isinstance(self.collector, NvidiaSmiCollector):
            self._gpu_name = self.collector.gpu_name()
        if self.collector.capability.status == "available":
            self._start_sampler()

    @property
    def capability(self) -> EnergyCapability:
        return self.collector.capability

    def _start_sampler(self) -> None:
        try:
            self.tracker.integrate(self.collector.sample())
        except Exception:
            return
        self._sampler_thread = threading.Thread(
            target=self._sample_loop,
            daemon=True,
            name="autose-energy-sampler",
        )
        self._sampler_thread.start()

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.sample_interval):
            try:
                sample = self.collector.sample()
            except Exception:
                continue
            self.tracker.integrate(sample)

    def stop(self) -> None:
        self._stop.set()
        if self._sampler_thread:
            self._sampler_thread.join(timeout=2)

    def begin(self, model: str, operation: str = "chat") -> str:
        return self.tracker.begin(model=model, operation=operation)

    def end(
        self,
        span_id: str,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        prompt_eval_duration_ns: int | None = None,
        eval_duration_ns: int | None = None,
        total_duration_ns: int | None = None,
        model_meta: dict[str, str | None] | None = None,
    ) -> EnergyResult | None:
        sensor_ok = self.collector.capability.status == "available"
        if sensor_ok:
            result = self.tracker.end_measured(
                span_id,
                collector=self.collector.capability.collector,
                scope=self.collector.capability.scope,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                sensor_available=True,
            )
            if result is not None and result.energy_joules is not None:
                return result
            # Sensor path produced no joules — fall through to approximation.
            if result is None:
                # Span missing; still try approximation with synthetic span metadata.
                started_at = datetime.now(UTC).isoformat()
                duration_ms = 0
                model = "unknown"
                operation = "chat"
            else:
                started_at = result.started_at
                duration_ms = result.duration_ms
                model = result.model
                operation = result.operation
        else:
            span = self.tracker.discard(span_id)
            if span is None:
                return None
            started_at = span.started_wall.isoformat()
            duration_ms = max(0, round((time.monotonic() - span.started_monotonic) * 1000))
            model = span.model
            operation = span.operation

        meta = model_meta or {}
        signals = parse_model_signals(
            model,
            parameter_size=meta.get("parameter_size"),
            quantization_level=meta.get("quantization_level"),
            family=meta.get("family"),
        )
        approx = approximate_energy(
            span_id=span_id,
            model=model,
            operation=operation,
            started_at=started_at,
            duration_ms=duration_ms,
            timings=TimingSignals(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                prompt_eval_duration_ns=prompt_eval_duration_ns,
                eval_duration_ns=eval_duration_ns,
                total_duration_ns=total_duration_ns,
            ),
            model_signals=signals,
            gpu_name=self._gpu_name,
        )
        self.tracker.record_external(approx)
        return approx

    def cache_model_meta(self, model: str, meta: dict[str, str | None]) -> None:
        with self._lock:
            self._model_cache[model] = meta

    def get_model_meta(self, model: str) -> dict[str, str | None] | None:
        with self._lock:
            return self._model_cache.get(model)

    def status(self) -> dict[str, Any]:
        capability = self.capability
        summary = self.tracker.summary(capability.to_dict())
        if capability.status == "available":
            return {
                "status": capability.status,
                "quality": capability.quality,
                "scope": capability.scope,
                "message": capability.message,
                "capability": capability.to_dict(),
                "summary": summary,
                "approximation_available": True,
            }
        return {
            "status": "approximating",
            "quality": "approximated",
            "scope": "modeled_active_device",
            "message": capability.message,
            "capability": capability.to_dict(),
            "summary": summary,
            "approximation_available": True,
        }


_default_monitor: EnergyMonitor | None = None
_default_lock = threading.Lock()


def get_default_monitor() -> EnergyMonitor:
    global _default_monitor
    with _default_lock:
        if _default_monitor is None:
            _default_monitor = EnergyMonitor()
        return _default_monitor


def reset_default_monitor_for_tests() -> None:
    global _default_monitor
    with _default_lock:
        if _default_monitor is not None:
            _default_monitor.stop()
        _default_monitor = None
