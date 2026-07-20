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
    model_load_scale,
    parse_model_signals,
)
from energy.ollama_meta import fetch_model_meta, ollama_native_root  # noqa: E402
from energy.platform_probe import DeviceIdentity  # noqa: E402


class ModelScaleTests(unittest.TestCase):
    def test_larger_model_has_higher_load_scale(self) -> None:
        small = parse_model_signals("a", parameter_size="7B", quantization_level="Q4_0")
        large = parse_model_signals("b", parameter_size="70B", quantization_level="Q4_0")
        self.assertGreater(model_load_scale(large), model_load_scale(small))

    def test_same_time_larger_model_uses_more_energy(self) -> None:
        device = DeviceIdentity(
            platform_name="darwin",
            architecture="arm64",
            hw_model="Mac16,12",
            chip_name="Apple M4",
        )
        timings = TimingSignals(
            prompt_tokens=40,
            completion_tokens=200,
            prompt_eval_duration_ns=300_000_000,
            eval_duration_ns=10_000_000_000,
        )
        small = approximate_energy(
            span_id="s",
            model="7b",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=10300,
            timings=timings,
            model_signals=parse_model_signals(
                "7b", parameter_size="7B", quantization_level="Q4_K_M"
            ),
            device=device,
        )
        large = approximate_energy(
            span_id="l",
            model="70b",
            operation="chat",
            started_at="2026-01-01T00:00:00+00:00",
            duration_ms=10300,
            timings=timings,
            model_signals=parse_model_signals(
                "70b", parameter_size="70B", quantization_level="Q4_K_M"
            ),
            device=device,
        )
        self.assertGreater(large.energy_joules or 0, small.energy_joules or 0)
        self.assertGreater((large.energy_joules or 0) / (small.energy_joules or 1), 1.5)

    def test_moe_uses_active_params_for_scale(self) -> None:
        moe = parse_model_signals("mixtral:8x7b", quantization_level="Q4_0")
        dense_total = parse_model_signals("x", parameter_size="56B", quantization_level="Q4_0")
        dense_active = parse_model_signals("y", parameter_size="14B", quantization_level="Q4_0")
        self.assertTrue(moe.is_moe)
        self.assertLess(model_load_scale(moe), model_load_scale(dense_total))
        self.assertGreater(model_load_scale(moe), model_load_scale(dense_active) * 0.9)


class OllamaMetaTests(unittest.TestCase):
    def test_native_root_from_openai_base(self) -> None:
        self.assertEqual(
            ollama_native_root("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434",
        )
        self.assertIsNone(ollama_native_root("http://10.2.57.222:9000/v1"))

    def test_fetch_model_meta_parses_show(self) -> None:
        payload = {
            "details": {
                "parameter_size": "11.9B",
                "quantization_level": "Q4_K_M",
                "family": "gemma4",
            },
            "model_info": {"general.parameter_count": 11_900_000_000},
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                import json

                return json.dumps(payload).encode()

        with patch("energy.ollama_meta.urllib.request.urlopen", return_value=_Resp()):
            meta = fetch_model_meta("http://127.0.0.1:11434/v1", "gemma4:12b")
        self.assertEqual(meta["parameter_size"], "11.9B")
        self.assertEqual(meta["quantization_level"], "Q4_K_M")
        self.assertEqual(meta["family"], "gemma4")
        self.assertEqual(meta["parameter_count"], 11_900_000_000)


if __name__ == "__main__":
    unittest.main()
