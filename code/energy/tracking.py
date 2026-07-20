"""Wire EnergyMonitor around agent._call_sync for CLI / desktop clients."""

from __future__ import annotations

import time
from typing import Any, Callable

from .format import format_energy_result
from .monitor import EnergyMonitor, get_default_monitor
from .ollama_meta import extract_native_timings, fetch_model_meta


def install_energy_tracking(
    agent: object,
    *,
    monitor: EnergyMonitor | None = None,
    on_result: Callable[[Any], None] | None = None,
) -> EnergyMonitor:
    """Wrap ``agent._call_sync`` with begin/end energy spans. Returns the monitor."""
    energy = monitor or get_default_monitor()
    original = agent._call_sync
    model_name = str(getattr(agent, "_model", "unknown") or "unknown")
    base_url = str(getattr(agent, "_base_url", "") or "")

    def tracked(messages, tools=None):
        span_id = energy.begin(model=model_name, operation="chat")
        started = time.monotonic()
        try:
            response = original(messages, tools=tools)
        except Exception:
            # Failed calls should not contribute ~0 J noise to the session total.
            energy.tracker.discard(span_id)
            raise

        elapsed_ns = max(0, int((time.monotonic() - started) * 1_000_000_000))
        usage = response.get("usage") if isinstance(response, dict) else None
        prompt_tokens = None
        completion_tokens = None
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")

        native = extract_native_timings(response if isinstance(response, dict) else None)
        # Prefer wall-clock for OpenAI-compat: native total_duration can include load.
        total_duration_ns = elapsed_ns

        meta = energy.get_model_meta(model_name)
        if meta is None:
            meta = fetch_model_meta(base_url, model_name)
            energy.cache_model_meta(model_name, meta)

        result = energy.end(
            span_id,
            prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
            completion_tokens=(
                completion_tokens if isinstance(completion_tokens, int) else None
            ),
            load_duration_ns=None,
            prompt_eval_duration_ns=native["prompt_eval_duration_ns"],
            eval_duration_ns=native["eval_duration_ns"],
            total_duration_ns=total_duration_ns,
            model_meta=meta,
        )
        if result is not None and on_result is not None:
            on_result(result)
        return response

    agent._call_sync = tracked  # type: ignore[method-assign]
    return energy


def result_to_event_data(result: Any) -> dict[str, Any]:
    return {
        "energy_joules": result.energy_joules,
        "energy_joules_lower": result.energy_joules_lower,
        "energy_joules_upper": result.energy_joules_upper,
        "quality": result.quality,
        "scope": result.scope,
        "method": result.method,
        "confidence": result.confidence,
        "collector": result.collector,
        "hardware_bucket": result.hardware_bucket,
        "calibration_id": result.calibration_id,
        "display": format_energy_result(result),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }
