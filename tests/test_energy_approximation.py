from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CODE_DIR = ROOT / "code"
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from energy.approximation import (  # noqa: E402
    TimingSignals,
    approximate_energy,
    model_bytes_gib,
    parse_model_signals,
)
from energy.ollama_meta import (  # noqa: E402
    extract_native_timings,
    fetch_model_meta,
    ollama_native_root,
)
from energy.platform_probe import DeviceIdentity  # noqa: E402


def _m4() -> DeviceIdentity:
    return DeviceIdentity(
        platform_name="darwin",
        architecture="arm64",
        hw_model="Mac16,12",
        chip_name="Apple M4",
    )


class ModelBytesTests(unittest.TestCase):
    def test_prefers_ollama_size_bytes(self) -> None:
        signals = parse_model_signals(
            "m",
            parameter_size="70B",
            quantization_level="Q4_0",
            size_bytes=5 * (1024**3),
        )
        self.assertAlmostEqual(model_bytes_gib(signals), 5.0, places=3)

    def test_larger_blob_uses_more_energy_same_time(self) -> None:
        timings = TimingSignals(
            prompt_tokens=40,
            completion_tokens=200,
            total_duration_ns=10_000_000_000,
        )
        small = approximate_energy(
            span_id="s",
            model="small",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=10_000,
            timings=timings,
            model_signals=parse_model_signals("small", size_bytes=3 * (1024**3)),
            device=_m4(),
        )
        large = approximate_energy(
            span_id="l",
            model="large",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=10_000,
            timings=timings,
            model_signals=parse_model_signals("large", size_bytes=12 * (1024**3)),
            device=_m4(),
        )
        self.assertGreater(large.energy_joules or 0, small.energy_joules or 0)

    def test_missing_phase_timings_widen_bounds(self) -> None:
        with_phase = approximate_energy(
            span_id="p",
            model="m",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=10_000,
            timings=TimingSignals(
                prompt_tokens=40,
                completion_tokens=200,
                prompt_eval_duration_ns=500_000_000,
                eval_duration_ns=9_500_000_000,
            ),
            model_signals=parse_model_signals("m", size_bytes=6 * (1024**3)),
            device=_m4(),
        )
        wall_only = approximate_energy(
            span_id="w",
            model="m",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=10_000,
            timings=TimingSignals(
                prompt_tokens=40,
                completion_tokens=200,
                total_duration_ns=10_000_000_000,
            ),
            model_signals=parse_model_signals("m", size_bytes=6 * (1024**3)),
            device=_m4(),
        )
        phase_span = (with_phase.energy_joules_upper or 0) - (with_phase.energy_joules_lower or 0)
        wall_span = (wall_only.energy_joules_upper or 0) - (wall_only.energy_joules_lower or 0)
        self.assertGreater(wall_span, phase_span)

    def test_calibration_v5(self) -> None:
        result = approximate_energy(
            span_id="v",
            model="m",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=1000,
            timings=TimingSignals(total_duration_ns=1_000_000_000),
            model_signals=parse_model_signals("m", size_bytes=1024**3),
            device=_m4(),
        )
        self.assertTrue((result.calibration_id or "").startswith("5:"))
        self.assertEqual(result.hardware_bucket, "apple_base")


class OllamaMetaTests(unittest.TestCase):
    def test_native_root_from_openai_base(self) -> None:
        self.assertEqual(
            ollama_native_root("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434",
        )
        self.assertIsNone(ollama_native_root("http://10.2.57.222:9000/v1"))

    def test_fetch_model_meta_parses_show_and_tags(self) -> None:
        show_payload = {
            "details": {
                "parameter_size": "11.9B",
                "quantization_level": "Q4_K_M",
                "family": "gemma4",
            },
            "model_info": {"general.parameter_count": 11_900_000_000},
        }
        tags_payload = {
            "models": [
                {"name": "gemma4:12b", "size": 8_000_000_000},
                {"name": "other:latest", "size": 1},
            ]
        }

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                import json

                return json.dumps(self._payload).encode()

        def fake_urlopen(request, timeout=0):
            url = getattr(request, "full_url", None) or request.get_full_url()
            if url.endswith("/api/show"):
                return _Resp(show_payload)
            if url.endswith("/api/tags"):
                return _Resp(tags_payload)
            raise AssertionError(url)

        with patch("energy.ollama_meta.urllib.request.urlopen", side_effect=fake_urlopen):
            meta = fetch_model_meta("http://127.0.0.1:11434/v1", "gemma4:12b")
        self.assertEqual(meta["parameter_size"], "11.9B")
        self.assertEqual(meta["quantization_level"], "Q4_K_M")
        self.assertEqual(meta["family"], "gemma4")
        self.assertEqual(meta["parameter_count"], 11_900_000_000)
        self.assertEqual(meta["size_bytes"], 8_000_000_000)

    def test_extract_native_timings(self) -> None:
        timings = extract_native_timings(
            {
                "load_duration": 100,
                "prompt_eval_duration": 200,
                "eval_duration": 300,
                "total_duration": 600,
            }
        )
        self.assertEqual(timings["load_duration_ns"], 100)
        self.assertEqual(timings["prompt_eval_duration_ns"], 200)
        self.assertEqual(timings["eval_duration_ns"], 300)
        self.assertEqual(timings["total_duration_ns"], 600)


if __name__ == "__main__":
    unittest.main()
